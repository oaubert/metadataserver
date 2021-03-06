#! /usr/bin/python

#
# Copyright (C) 2014 Olivier Aubert <contact@olivieraubert.net>
#
# This file is part of MetaDataServer
#
# MetaDataServer is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# MetaDataServer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with MetaDataServer.  If not, see <http://www.gnu.org/licenses/>.
#
"""MetaDataServer - a simple server for MetaDataPlayer

This is a basic implementation of a read/write data server for the
MetaDataPlayer component from IRI.
It stores the annotation information in a mongo database.
"""

import sys
import os
import json
import bson
import uuid
import time
import datetime
from functools import wraps, update_wrapper
from optparse import OptionParser
import smtplib
from email.mime.text import MIMEText

import jsonschema
import pymongo

from flask import Flask, Response, render_template, make_response
from flask import session, request, redirect, url_for, current_app, abort, jsonify

# PARAMETERS
API_PREFIX = '/api/'

# Server configuration
CONFIG = {
    'database': 'mds',
    # Enable debug.
    'enable_debug': False,
    'enable_cross_site_requests': False,
    'port': 5001,
}

connection = pymongo.MongoClient("localhost", 27017)

app = Flask(__name__)

DEFAULT_KEY = 'default'
APIKEYS = {}

CORS_HEADERS = [ "origin",
                 "content-type",
                 "accept" ]
# List of validating schemas
SCHEMAS = {
    'analytics': {
        "type" : "object",
        "properties" : {
            #         # date, username, user-uuid, object-url, property, value (literal)
            "date" : { "type": "string" }, # should be date-time
            "username" : { "type" : "string" },
            "useruuid" : { "type" : "string" },
            "subject": { "type": "string" },
            "property": { "type": "string" },
            "value": { "anyOf": [
                {"type": "string", "maxLength": 255},
                {"type": "integer" }
            ]
            }
        },
    },
    'key': {
        "type" : "object",
        "properties" : {
            "key" : { "type": "string" },
            "capabilities" : { "type": "array",
                               "items": {
                                   "type": "string"
                               }
            }
        }
    },
}

class InvalidAccess(Exception):
    pass

def get_api_key():
    """Return the request API key
    """
    key = request.headers.get('x-apikey') or DEFAULT_KEY
    # Allow to override with parameter
    param =  request.values.get('apikey')
    if param and param != DEFAULT_KEY:
        key = param
    return key

def connect_db():
    global db
    db = connection[CONFIG['database']]

@app.errorhandler(InvalidAccess)
def handle_invalid_access(error):
    return make_response("Invalid API key", 403)

def check_access(elements=[], use_collection=False, use_stripped_collection=False):
    """Decorator that checks access rights.
    """
    def _wrapper(original_function):
        def _checker(*args, **kwargs):
            if use_collection and 'collection' in kwargs:
                els = [ kwargs.get('collection') ] + list(elements)
            elif use_stripped_collection and 'collection' in kwargs:
                els = [ kwargs.get('collection', '').rstrip('s') ] + list(elements)
            else:
                els = elements
            if not check_capability(get_api_key(), [ "%s%s" % (request.method, el) for el in els ]):
                raise InvalidAccess()
            return original_function(*args, **kwargs)
        return wraps(original_function)(_checker)
    return _wrapper

# Snippet from http://flask.pocoo.org/snippets/56/
def crossdomain(origin=None, methods=None, headers=None,
                max_age=21600, attach_to_all=True,
                automatic_options=True):
    if methods is not None:
        methods = ', '.join(sorted(x.upper() for x in methods))
    if headers is not None and not isinstance(headers, basestring):
        headers = ', '.join(x.upper() for x in headers)
    if not isinstance(origin, basestring):
        origin = ', '.join(origin)
    if isinstance(max_age, datetime.timedelta):
        max_age = max_age.total_seconds()

    def get_methods():
        if methods is not None:
            return methods

        options_resp = current_app.make_default_options_response()
        return options_resp.headers['allow']

    def decorator(f):
        def wrapped_function(*args, **kwargs):
            print "Checking wrapper CORS"
            if automatic_options and request.method == 'OPTIONS':
                resp = current_app.make_default_options_response()
            else:
                resp = make_response(f(*args, **kwargs))
            if not attach_to_all and request.method != 'OPTIONS':
                return resp
            if not CONFIG['enable_cross_site_requests']:
                return resp

            h = resp.headers

            h['Access-Control-Allow-Origin'] = origin
            h['Access-Control-Allow-Methods'] = get_methods()
            h['Access-Control-Max-Age'] = str(max_age)
            if headers is not None:
                h['Access-Control-Allow-Headers'] = headers
            print "CORS wrapped"
            return resp

        f.provide_automatic_options = False
        print "Wrapping"
        return update_wrapper(wrapped_function, f)
    return decorator

def validate_schema(data, schemaid):
    # Check data structure, using jsonschema
    try:
        jsonschema.validate(data, SCHEMAS[schemaid])
    except (jsonschema.ValidationError, jsonschema.SchemaError), e:
        # Unprocessable entity
        abort(422, e.message)

def load_keys():
    """Load API key data.
    """
    global APIKEYS
    APIKEYS = {}
    for k in list(db['apikeys'].find()):
        APIKEYS[k['key']] = set(str(c) for c in k['capabilities'])

def check_capability(key, actions):
    """Check that the given key is authorized to execute the given actions

    actions is a list (or set) of action identifier. There are generic
    actions built from the request method and parameters, and specific
    action with specific identifiers.
    POSTelement
    PUTelement
    DELETEelement
    DELETEannotation
    etc...

    Admin rights correspond to:
    ['GETadmin', 'GETelements', 'GETelement', 'PUTelement', 'DELETEelement', 'POSTelements', 'POSTelement', 'GETunfilteredelements', 'GETkeys', 'POSTkeys', 'GETkey', 'PUTkey', 'DELETEkey' ]
    """
    if CONFIG.get('enable_debug'):
        app.logger.debug("Check %s : %s <-> %s", request.path, unicode(actions), unicode(APIKEYS.get(key)))
    return set(actions).intersection(APIKEYS.get(key, []))

class MongoEncoder(json.JSONEncoder):
    def default(self, obj, **kwargs):
        if isinstance(obj, bson.ObjectId):
            return str(obj)
        else:
            return json.JSONEncoder.default(obj, **kwargs)

def jsonp(func):
    """Wraps JSONified output for JSONP requests.

    From http://flask.pocoo.org/snippets/79/
    """
    @wraps(func)
    def decorated_function(*args, **kwargs):
        callback = request.args.get('callback', False)
        if callback:
            data = str(func(*args, **kwargs).data)
            content = str(callback) + '(' + data + ')'
            mimetype = 'application/javascript'
            return current_app.response_class(content, mimetype=mimetype)
        else:
            return func(*args, **kwargs)
    return decorated_function

def fix_ids(data, mapping=None):
    """Generate UUIDs when importing data with possibly non-unique ids.
    """
    if mapping is None:
        mapping = {}
    if not 'id' in data:
        data['id'] = str(uuid.uuid1())
    elif len(data['id']) < 4:
        # It is not a UUID, generate one
        old = data['mds:oldid'] = data['id']
        mapping[old] = data['id'] = str(uuid.uuid1())
    return data

def clean_json(data, mapping=None):
    """Clean the input json.

    - Mongo does not accept dots in attribute names.
    - Convert mapped ids
    """
    if mapping is None:
        mapping = {}
    # Fix ids for all elements
    fix_ids(data, mapping)
    # For media
    if 'http://advene.liris.cnrs.fr/ns/frame_of_reference/ms' in data:
        del data['http://advene.liris.cnrs.fr/ns/frame_of_reference/ms']
    # For any element
    meta = data.get('meta', data)
    # Generate created/modified if needed
    for n in ('dc:created', 'dc:modified'):
        if not meta.get(n):
            meta[n] = datetime.datetime.now().isoformat()
    # Author Metadata is in the meta dict (annotation, media), or in the dict itself (annotationtype, package)
    for n in ('dc:created.contents', 'dc:creator.contents'):
        if n in meta:
            meta[n.replace('.', '_')] = meta[n]
            del meta[n]
    # Convert mapped ids (for annotations)
    newidref = mapping.get(meta.get('id-ref'), meta.get('id-ref'))
    if newidref is not None:
        meta['id-ref'] = newidref
    newmedia = mapping.get(data.get('media'), data.get('media'))
    if newmedia:
        data['media'] = newmedia

    return data

def restore_json(data):
    """Restore valid json from a cleaned json.
    """
    if data is None:
        return None
    # For any element
    meta = data.get('meta', {})
    for n in ('dc:created_contents', 'dc:creator_contents'):
        if n in meta:
            meta[n.replace('_', '.')] = meta[n]
            del meta[n]
    if "unit" in data:
        data["http://advene.liris.cnrs.fr/ns/frame_of_reference/ms"] = "o=0"
        data["origin"] = 0
        meta['dc:duration'] = long(meta['dc:duration'])

    return data

def uncolon(data):
    """Remove colons from data property names.

    jinja/mustache templates do not allow to use colons in expressions.
    """
    for n,v in list(data.iteritems()):
        if ':' in n:
            data[n.replace(':', '_')] = v
        if isinstance(v, dict):
            uncolon(v)
    return data

def normalize_annotation(data):
    """Fill missing data for created annotations.

    Modify structure in-place.
    """
    m = data['meta']
    if 'created' in m:
        m['dc:created'] = m['dc:modified'] = m['created']
        m['dc:creator'] = m['dc:contributor'] = m['creator']
        del m['creator'], m['created']
    if not 'dc:created' in m:
        m['dc:created'] = m['dc:modified'] = datetime.datetime.now().isoformat()
        # FIXME: get creator/contributor for session info
        m['dc: creator'] = m['dc:contributor'] = 'system'
    if not 'id-ref' in m and 'type_title' in data:
        at = restore_json(db['annotationtypes'].find_one({ 'dc:title': data['type_title'] }))
        if not at:
            # Automatically create missing annotation type
            at = clean_json({
                "dc:contributor": "system",
                "dc:creator": "system",
                "dc:title": data['type_title'],
                "dc:description": "",
                "dc:modified": m['dc:created'],
                "dc:created": m['dc:created']
            })
            db['annotationtypes'].save(at)
        m['id-ref'] = at['id']

@app.errorhandler(401)
def custom_401(error):
    return Response('Unauthorized access', 401, {'WWWAuthenticate':'Basic realm="Login Required"'})

@app.route("/", methods= [ 'GET', 'HEAD', 'OPTIONS' ])
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ])
def index():
    if request.method == 'HEAD' or request.method == 'OPTIONS':
        return Response('', 200);

    if not 'userinfo' in session:
        # Autologin
        session['userinfo'] = { 'login': 'anonymous' }
        session['userinfo'].setdefault('id', str(uuid.uuid1()))
        db['userinfo'].save(dict(session['userinfo']))
    return render_template('index.html', userinfo=session.get('userinfo'), key=get_api_key())

@app.route("/package/")
@check_access(('elements', 'packages'))
@crossdomain(origin='*', methods= [ 'GET', 'HEAD', 'OPTIONS' ])
def packages_view():
    packages = list(db['packages'].find())
    for p in packages:
        uncolon(p)
        media = db['medias'].find_one({'id': p['main_media']['id-ref']})
        if media:
            p['main_media'].update(uncolon(media))
        p['annotations'] = list(db['annotations'].find({'media': p['main_media']['id-ref']}))
        for a in p['annotations']:
            uncolon(a)
    return render_template('packages.html', packages=packages, key=get_api_key())

@app.route("/package/<string:pid>/")
@check_access(('element', 'package'))
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ])
def package_view(pid):
    package = db['packages'].find_one({ 'id': pid })
    if package is None:
        abort(404)
    media = db['medias'].find_one({ 'id': package['main_media']['id-ref'] })
    return render_template('package.html', package=uncolon(package), media=media, key=get_api_key())

@app.route("/package/<string:pid>/imagecache/<path:info>")
def imagecache_view(pid, info):
    return redirect('/static/imagecache/%s/%s' % (pid, info), code=301)

@app.route("/admin/")
@check_access(('admin',))
def admin_view():
    return render_template('admin.html', filter=request.values.get('filter', ''), key=get_api_key())

@app.route("/moderate/")
@check_access(('moderate', 'admin'))
def moderate_view():
    mediainfo = [ (r['_id'], r['annotations'], r['lastmod'])
                  for r in db.annotations.aggregate([
                          { '$group': {
                              '_id': '$media',
                              'annotations': { '$sum': 1 },
                              'lastmod': { '$max': '$meta.dc:modified'}
                          }
                        }]) ]
    return render_template('moderate.html', filter=request.values.get('filter', ''), mediainfo=mediainfo, key=get_api_key())

@app.route('/login', methods = ['GET', 'POST'])
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ])
def login():
    return render_template('login.html')

# Specific fields that can be used to filter elements, in addition to the standard ones (user)
SPECIFIC_QUERYMAPS = {
    'annotations': {
        'media': 'media',
        'type': 'meta.id-ref',
    },
    'medias': {
        'url': 'url',
    },
    'annotationtypes': {
    },
    'userinfo': {
    },
    'packages': {
        'media': 'main_media.id-ref'
    }
}

@app.route(API_PREFIX + 'annotation', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ], defaults={'collection': 'annotations'})
@app.route(API_PREFIX + 'annotationtype', methods= [ 'GET', 'POST' ], defaults={'collection': 'annotationtypes'})
@app.route(API_PREFIX + 'media', methods= [ 'GET', 'POST' ], defaults={'collection': 'medias'})
@app.route(API_PREFIX + 'userinfo', methods= [ 'GET', 'POST' ], defaults={'collection': 'userinfo'})
@app.route(API_PREFIX + 'meta', methods= [ 'GET', 'POST' ], defaults={'collection': 'packages'})
@check_access(('elements', ), use_collection=True)
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ], headers=CORS_HEADERS)
def element_list(collection):
    """Generic element listing method.

    It handles GET and POST requests on element collections.
    """
    # TODO: find a way to pass collection parameter to check_access
    if request.method == 'HEAD' or request.method == 'OPTIONS':
        if CONFIG['enable_cross_site_requests']:
            return Response('', 200, {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
            })
        else:
            return Response('', 200);

    if request.method == 'POST':
        # FIXME: do some sanity checks here (valid properties, existing ids...)
        # Insert a new element
        data = request.json
        if collection == 'annotations':
            normalize_annotation(data)
        db[collection].save(clean_json(data))
        response = current_app.response_class( json.dumps(restore_json(data),
                                                          indent=None if request.is_xhr else 2,
                                                          cls=MongoEncoder),
                                               mimetype='application/json')
        if CONFIG['enable_cross_site_requests']:
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    else:
        querymap = { 'user': 'meta.dc:contributor',
                     'creator': 'meta.dc:creator' }
        querymap.update(SPECIFIC_QUERYMAPS[collection])
        if (not request.values.getlist('filter')
            and not check_capability(get_api_key(),
                                     [ "GETunfiltered%s" % el for el in ('elements', collection) ])):
            raise InvalidAccess("Query too generic.")

        query = dict( (querymap.get(name, name), value)
                      for (name, value) in ( f.split(':') for f in request.values.getlist('filter') )
                      if name in querymap
        )
        cursor = db[collection].find(query)
        response = current_app.response_class( json.dumps(list(restore_json(a) for a in cursor),
                                                          indent=None if request.is_xhr else 2,
                                                          cls=MongoEncoder),
                                               mimetype='application/json')
        if CONFIG['enable_cross_site_requests']:
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

@app.route(API_PREFIX + 'annotation/<string:eid>', methods= [ 'GET', 'PUT', 'DELETE' ], defaults={'collection': 'annotations'})
@app.route(API_PREFIX + 'annotationtype/<string:eid>', methods= [ 'GET', 'PUT', 'DELETE' ], defaults={'collection': 'annotationtypes'})
@app.route(API_PREFIX + 'media/<string:eid>', methods= [ 'GET', 'PUT', 'DELETE' ], defaults={'collection': 'medias'})
@app.route(API_PREFIX + 'userinfo/<string:eid>', methods= [ 'GET', 'PUT', 'DELETE' ], defaults={'collection': 'userinfo'})
@app.route(API_PREFIX + 'meta/<string:eid>', methods= [ 'GET', 'PUT', 'DELETE' ], defaults={'collection': 'packages'})
@check_access(('element', ), use_stripped_collection=True)
@crossdomain(origin='*', methods= [ 'GET', 'PUT', 'DELETE', 'OPTIONS' ], headers=CORS_HEADERS)
def element_get(eid, collection):
    """Generic element access.

    It handles GET/PUT/DELETE requests on element instances.
    Note that /package/ is handled on its own, since we regenerate data by aggregating different elements.
    """
    el = db[collection].find_one({ 'id': eid })
    if el is None:
        abort(404)
    if request.method == 'DELETE':
        # FIXME Do some sanity checks before deleting
        db[collection].remove({ 'id': eid }, True)
    elif request.method == 'PUT':
        # FIXME Do some sanity checks before storing
        if request.headers.get('content-type') == 'application/json':
            data = json.loads(request.data)
            if data['id'] != el['id']:
                abort(409, "Mismatching element")
            data['_id'] = el['_id']
            if collection == 'annotations':
                # Fix missing/wrong fields
                normalize_annotation(data)
            db[collection].save(clean_json(data))
            return make_response(json.dumps(data, cls=MongoEncoder), 201)
        abort(415)
    return current_app.response_class(json.dumps(el, indent=None if request.is_xhr else 2, cls=MongoEncoder),
                                      mimetype='application/json')

@app.route(API_PREFIX + 'user/', methods= [ 'GET' ])
@check_access(('elements', 'users'))
def user_list():
    """Enumerate contributing users.
    """
    users = { }
    for collection in ('annotations', 'medias', 'packages', 'annotationtypes'):
        if collection in ('annotations', 'medias'):
            field = '$meta.dc:contributor'
        else:
            field = '$dc:contributor'
        aggr = db[collection].aggregate( [ { '$group': { '_id': field, 'count': { '$sum': 1 } } } ] )
        for res in aggr:
            users.setdefault(res['_id'], {})[collection] = res['count']
    return current_app.response_class( json.dumps(users,
                                       indent=None if request.is_xhr else 2,
                                       cls=MongoEncoder),
                                       mimetype='application/json')

@app.route(API_PREFIX + 'key/', methods= [ 'GET', 'POST' ])
@check_access(('admin', 'keys'))
def key_list():
    """Enumerate API keys.
    """
    if request.method == 'POST':
        # Create a new key
        data = json.loads(request.data)
        if data.get('key') and data.get('capabilities'):
            key = data.get('key')
            el = db['apikeys'].find_one({ 'key': key })
            if el is not None:
                # Key already existing. Should use update.
                abort(409)
            caps = data.get('capabilities')
            # Let's handle both restAdmin serialization and raw edition
            if isinstance(caps, basestring):
                caps = caps.split(",")
            data = { 'key': key,
                     'capabilities': caps }
            validate_schema(data, 'key')
            db['apikeys'].insert(data)
            load_keys()
            return current_app.response_class( json.dumps(data,
                                               indent=None if request.is_xhr else 2,
                                               cls=MongoEncoder),
                                               mimetype='application/json')
        else:
            abort(401)
    else:
        return current_app.response_class( json.dumps(list(db['apikeys'].find()),
                                                      indent=None if request.is_xhr else 2,
                                                      cls=MongoEncoder),
                                           mimetype='application/json')

@app.route(API_PREFIX + 'key/<string:k>', methods= [ 'GET', 'PUT', 'DELETE' ])
@check_access(('admin', 'key'))
def key_get(k):
    """Key access

    It handles GET/PUT/DELETE requests on API keys
    """
    el = db['apikeys'].find_one({ 'key': k })
    if el is None:
        abort(404)
    if request.method == 'DELETE':
        # FIXME Do some sanity checks before deleting
        db['apikeys'].remove({ 'key': k }, True)
        load_keys()
    elif request.method == 'PUT':
        # FIXME Do some sanity checks before storing
        if request.headers.get('content-type') == 'application/json':
            data = json.loads(request.data)
            if data['key'] != el['key']:
                abort(409)
            data['_id'] = el['_id']
            # Let's handle both restAdmin serialization and raw edition
            if isinstance(data['capabilities'], basestring):
                data['capabilities'] = data['capabilities'].split(',')
            validate_schema(data, 'key')
            db['apikeys'].save(data)
            load_keys()
            return make_response(json.dumps(data, cls=MongoEncoder), 201)
        abort(415)
    # GET
    return current_app.response_class(json.dumps(el, indent=None if request.is_xhr else 2, cls=MongoEncoder),
                                      mimetype='application/json')

@app.route(API_PREFIX + 'analytics/', methods= [ 'GET', 'POST', 'OPTIONS' ])
@check_access(('admin', 'analytics'))
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ], headers=CORS_HEADERS)
def analytics_list():
    """Enumerate analytics objects
    """
    if request.method == 'POST':
        # Create a new key
        data = json.loads(request.data)
        data['date'] = datetime.datetime.now().isoformat()
        validate_schema(data, 'analytics')
        # date, username, useruuid, subject, property, value
        db['analytics'].insert(data)
        return current_app.response_class( json.dumps(data,
                                                      indent=None if request.is_xhr else 2,
                                                      cls=MongoEncoder),
                                        mimetype='application/json')
    else:
        # FIXME: handle query parameters (username/suject/property)
        return current_app.response_class( json.dumps(list(db['analytics'].find()),
                                                      indent=None if request.is_xhr else 2,
                                                      cls=MongoEncoder),
                                           mimetype='application/json')

@app.route(API_PREFIX + 'analytics/<string:key>', methods= [ 'GET' ])
@check_access(('admin', 'analytics'))
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ], headers=CORS_HEADERS)
def analytics_get(key):
    """Analytics access

    It handles GET on analytics data
    """
    return current_app.response_class(json.dumps(list(db['analytics'].find({'subject': key})),
                                                 indent=None if request.is_xhr else 2, cls=MongoEncoder),
                                    mimetype='application/json')

@app.route(API_PREFIX + 'user/<string:uid>/annotation', methods= [ 'GET' ])
@check_access(('elements', 'userannotations'))
def user_annotation_list(uid):
    return current_app.response_class( json.dumps(list(db['annotations'].find({'meta.dc:creator': uid})),
                                                  indent=None if request.is_xhr else 2,
                                                  cls=MongoEncoder),
                                       mimetype='application/json')

@app.route(API_PREFIX + 'package/', methods= [ 'GET', 'POST' ])
@check_access(('elements', 'packages'))
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ], headers=CORS_HEADERS)
def package_list():
    if request.method == 'POST':
        # Insert a new package
        data = request.json

        # Mapping table for converted ids
        mapping = {}
        for m in data.get('medias', []):
            l = db['medias'].find({'id': m['id']})
            if l.count() == 0:
                # Not already existing media
                db['medias'].save(clean_json(m, mapping))
        for at in data.get('annotation-types', []):
            l = db['annotationtypes'].find({'dc:title': at['dc:title']})
            if l.count() == 0:
                # Not already existing type
                db['annotationtypes'].save(clean_json(at, mapping))
            else:
                # Remap with existing annotationtype id
                mapping[at['id']] = l[0]['id']
        for a in data.get('annotations', []):
            db['annotations'].save(clean_json(a, mapping))

        p = data['meta']

        # Interim hack for malformed data
        if isinstance(p['main_media'], basestring):
            # Malformed data. Replace by a dict.
            p['main_media'] = { 'id-ref': p['main_media'] }

        if not p.get('main_media') or p['main_media'].get('id-ref') == 'package1':
            # Use first media if no valid media was specified
            p['main_media']['id-ref'] = { 'id-ref': data['medias'][0]['id'] }

        newmediaref = mapping.get(p['main_media'].get('id-ref'))
        if newmediaref is not None:
            p['main_media']['id-ref'] = newmediaref

        fix_ids(p)
        # FIXME: there should be some way to specify associated media/annotationtypes/annotations.
        # Maybe store in meta some info containings ids?
        l = db['packages'].find_one({'id': p['id']})
        if not l:
            db['packages'].save(p)
        return jsonify(id=p['id'])
    else:
        querymap = { 'user': 'meta.dc:contributor',
                     'creator': 'meta.dc:creator',
                     # 'url': 'url'
                   }
        query = dict( (querymap.get(name, name), value)
                      for f in request.values.getlist('filter')
                      for name, value in f.split(':') )
        cursor = db['packages'].find(query)
        response = current_app.response_class( json.dumps(list(restore_json(m) for m in cursor),
                                                          indent=None if request.is_xhr else 2,
                                                          cls=MongoEncoder),
                                               mimetype='application/json')
        return response

@app.route(API_PREFIX + 'package/<string:pid>', methods= [ 'GET' ])
@check_access(('element', 'package'))
@crossdomain(origin='*', methods= [ 'GET', 'POST', 'HEAD', 'OPTIONS' ], headers=CORS_HEADERS)
def package_get(pid):
    meta = db['packages'].find_one({ 'id': pid })
    if meta is None:
        abort(404)
    p = restore_json({ 'meta': meta })
    # Fetch corresponding medias, annotation-types and annotations
    mid = p['meta']['main_media']['id-ref']
    media = db['medias'].find_one({ 'id': mid })
    if media is not None:
        p['medias'] = [ restore_json(media) ]
    annotations = db['annotations'].find({ 'media': mid })
    p['annotations'] = [ restore_json(a) for a in annotations ]
    ats = annotations.distinct('meta.id-ref')
    p['annotation-types'] = []
    # Dump all annotation types for now
    # FIXME: find a better solution to make sure Contributions is included
    for at in db['annotationtypes'].find():
        p['annotation-types'].append(restore_json(at))
    #for atid in ats:
    #    at = db['annotationtypes'].find_one({ 'id': atid })
    #    if at is not None:
    #        p['annotation-types'].append(restore_json(at))
    #    else:
    #        app.logger.info("Error: missing annotation type", atid)
    data = json.dumps(p, indent=None if request.is_xhr else 2, cls=MongoEncoder)
    mimetype = 'application/json'
    callback = request.args.get('callback', False)
    if callback:
        data = str(callback) + '(' + data + ')'
        mimetype = 'application/javascript'
    return current_app.response_class(data, mimetype=mimetype)

def send_email():
    fp = open(textfile, 'rb')
    # Create a text/plain message
    msg = MIMEText(fp.read())
    fp.close()

    # me == the sender's email address
    # you == the recipient's email address
    msg['Subject'] = 'The contents of %s' % textfile
    msg['From'] = me
    msg['To'] = you

    # Send the message via our own SMTP server, but don't include the
    # envelope header.
    s = smtplib.SMTP('localhost')
    s.sendmail(me, [you], msg.as_string())
    s.quit()

# set the secret key.  keep this really secret:
app.secret_key = os.urandom(24)

if __name__ == "__main__":
    global db
    parser = OptionParser(usage="""Trace server.\n%prog [options]""")

    parser.add_option("-D", "--database", dest="database", action="store", default="mds")
    parser.add_option("-d", "--debug", dest="enable_debug", action="store_true",
                      help="Enable debug mode.", default=False)
    parser.add_option("-x", "--cross-site-requests", dest="enable_cross_site_requests", action="store_true",
                      help="Enable cross site requests.", default=False)
    parser.add_option("-e", "--external", dest="allow_external_access", action="store_true",
                      help="Allow external access (from any host)", default=False)
    parser.add_option("-p", "--port", dest="port", type="int", action="store",
                      help="Port number", default=5001)
    parser.add_option("-K", "--admin-api-key", dest="admin_key", action="store", help="Store an admin API key", default=None)

    (options, args) = parser.parse_args()
    CONFIG.update(vars(options))
    if options.enable_debug:
        options.allow_external_access = False

    connect_db()

    if options.admin_key:
        db['apikeys'].insert({ 'key': options.admin_key,
                               'capabilities': "GETadmin,POSTadmin,GETelements,GETelement,PUTelement,POSTelements,DELETEelement,POSTelement,GETunfilteredelements,GETkeys,GETkey,PUTkey,DELETEkey,POSTkeys".split(',') })
        print "Key %s added as admin key. You can restart the server." % options.admin_key
        sys.exit(0)
    load_keys()

    if args and args[0] == 'shell':
        import pdb; pdb.set_trace()
        import sys; sys.exit(0)

    if CONFIG['enable_debug']:
        app.run(debug=True, port=CONFIG['port'])
    elif CONFIG['allow_external_access']:
        app.run(debug=False, host='0.0.0.0', port=CONFIG['port'])
    else:
        app.run(debug=False, port=CONFIG['port'])
