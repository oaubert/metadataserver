#! /usr/bin/python

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

import os
import json
import bson
import uuid
import time
import datetime
from optparse import OptionParser
from werkzeug.utils import cached_property
from werkzeug.wrappers import Request
from flask import Flask, Response, render_template, make_response
from flask import session, request, redirect, url_for, current_app, abort, jsonify
import pymongo

# PARAMETRES
# DB = DataBase
# COL= Collection
DB   	  = 'mds'

API_PREFIX = '/api/'

# Server configuration
CONFIG = {
    # Enable debug.
    'enable_debug': False,
}

connection = pymongo.Connection("localhost", 27017)
db = connection[DB]

app = Flask(__name__)

class MongoEncoder(json.JSONEncoder):
    def default(self, obj, **kwargs):
        if isinstance(obj, bson.ObjectId):
            return str(obj)
        else:
            return json.JSONEncoder.default(obj, **kwargs)

def fix_ids(data, mapping=None):
    if mapping is None:
        mapping = {}
    if not 'id' in data:
        data['id'] = str(uuid.uuid1())
    elif len(data['id']) < 36:
        # It is not a UUID, generate one
        old = data['mds:oldid'] = data['id']
        mapping[old] = data['id'] = str(uuid.uuid1())
    return data

def clean_json(data, mapping=None):
    """Clean the input json.

    Mongo does not accept dots in attribute names.
    """
    if mapping is None:
        mapping = {}
    # Fix ids for all elements
    fix_ids(data, mapping)
    # For media
    if 'http://advene.liris.cnrs.fr/ns/frame_of_reference/ms' in data:
        del data['http://advene.liris.cnrs.fr/ns/frame_of_reference/ms']
    # For any element
    # Author Metadata is in the meta dict (annotation, media), or in the dict itself (annotationtype, package)
    meta = data.get('meta', data)
    for n in ('dc:created.contents', 'dc:creator.contents'):
        if n in meta:
            meta[n.replace('.', '_')] = meta[n]
            del meta[n]
    # Convert mapped ids (for annotations)
    newidref = mapping.get(meta.get('id-ref'))
    if newidref is not None:
        meta['id-ref'] = newidref
    newmedia = mapping.get(data.get('media'))
    if newmedia:
        data['media'] = newmedia

    return data

def restore_json(data):
    """Restore valid json from a cleaned json
    """
    if data is None:
        return None
    # For any element
    meta = data.get('meta', {})
    for n in ('dc:created_contents', 'dc:creator_contents'):
        if n in meta:
            meta[n.replace('_', '.')] = meta[n]
            del meta[n]
    return data

def uncolon(data):
    for n,v in list(data.iteritems()):
        if ':' in n:
            data[n.replace(':', '_')] = v
        if isinstance(v, dict):
            uncolon(v)
    return data

def normalize_annotation(data):
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

@app.route("/")
def index():
    if 'userinfo' in session:
        #return 'Logged in as : %s' % escape(session['navigator'])
        #session['navigator']['id']="test";
        packages = list(db['packages'].find())
        for p in packages:
            uncolon(p)
            media = db['medias'].find_one({'id': p['main_media']['id-ref']})
            if media:
                p['main_media'].update(uncolon(media))
            p['annotations'] = list(db['annotations'].find({'media': p['main_media']['id-ref']}))
            for a in p['annotations']:
                uncolon(a)
        return render_template('index.html',
                               userinfo=session['userinfo'],
                               packages=packages
                           )
    return 'You are not logged in'

@app.route("/package/")
def packages_view():
    packages = list(db['packages'].find())
    for p in packages:
        p['media'] = db['medias'].find_one({ 'id': p['main_media']['id-ref'] })
    return render_template('packages.html', packages=packages)

@app.route("/package/<string:pid>/")
def package_view(pid):
    package = db['packages'].find_one({ 'id': pid })
    if package is None:
        abort(404)
    media = db['medias'].find_one({ 'id': package['main_media']['id-ref'] })
    return render_template('package.html', package=package, media=media)

@app.route("/package/<string:pid>/imagecache/<path:info>")
def imagecache_view(pid, info):
    return redirect('/static/imagecache/%s/%s' % (pid, info), code=301)

@app.route("/admin/")
def admin_view():
    return render_template('admin.html')

@app.route('/login', methods = ['GET', 'POST'])
def login():
    # 'userinfo' is either a (GET) named param, or a (POST) form
    # field, whose value contains JSON data with information about
    # the user
    params = request.values.get('default_user', '{"login": "anonymous"}')

    if 'userinfo' in session:
        # session was already initialized. Update its information.
        d = json.loads(params)
        d['id'] = session['userinfo']['id']
        db['userinfo'].update( {"id": session['userinfo']['id']}, d)
        session['userinfo'].update(d)
        session.modified = True
    else:
        session['userinfo'] = json.loads(params)
        session['userinfo'].setdefault('id', str(uuid.uuid1()))
        db['userinfo'].save(dict(session['userinfo']))
        session.modified = True

    # Current time in ms. It may be different from times sent by
    # client, because of different timezones or even clock skew. It is
    # indicative.
    t = long(time.time() * 1000)
    db['trace'].save({ '_serverid': session['userinfo'].get('id', ""),
                       '@type': 'Login',
                       'begin': t,
                       'end': t,
                       'subject': session['userinfo'].get('default_subject', "anonymous")
                       })
    #app.logger.debug("Logged in as " + session['userinfo']['id'])
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('userinfo', None)
    return redirect(url_for('index'))

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


@app.route(API_PREFIX + 'annotation', methods= [ 'GET', 'POST' ], defaults={'collection': 'annotations'})
@app.route(API_PREFIX + 'annotationtype', methods= [ 'GET', 'POST' ], defaults={'collection': 'annotationtypes'})
@app.route(API_PREFIX + 'media', methods= [ 'GET', 'POST' ], defaults={'collection': 'medias'})
@app.route(API_PREFIX + 'userinfo', methods= [ 'GET', 'POST' ], defaults={'collection': 'userinfo'})
@app.route(API_PREFIX + 'meta', methods= [ 'GET', 'POST' ], defaults={'collection': 'packages'})
def element_list(collection):
    if request.method == 'POST':
        # FIXME: do some sanity checks here (valid properties, existing ids...)
        # Insert a new element
        data = request.json
        if collection == 'annotations':
            normalize_annotation(data)
        db[collection].save(clean_json(request.json))
        return jsonify(id=data['id'])
    else:
        querymap = { 'user': 'meta.dc:contributor',
                     'creator': 'meta.dc:creator' }
        querymap.update(SPECIFIC_QUERYMAPS[collection])
        query = dict( (querymap.get(name, name), value)
                      for (name, value) in ( f.split(':') for f in request.values.getlist('filter') )
                      if 'name' in querymap
        )
        cursor = db[collection].find(query)
        response = current_app.response_class( json.dumps(list(restore_json(a) for a in cursor),
                                                          indent=None if request.is_xhr else 2,
                                                          cls=MongoEncoder),
                                               mimetype='application/json')
        return response

@app.route(API_PREFIX + 'annotation/<string:eid>', methods= [ 'GET', 'PUT' ], defaults={'collection': 'annotations'})
@app.route(API_PREFIX + 'annotationtype/<string:eid>', methods= [ 'GET', 'PUT' ], defaults={'collection': 'annotationtypes'})
@app.route(API_PREFIX + 'media/<string:eid>', methods= [ 'GET', 'PUT' ], defaults={'collection': 'medias'})
@app.route(API_PREFIX + 'userinfo/<string:eid>', methods= [ 'GET', 'PUT' ], defaults={'collection': 'userinfo'})
@app.route(API_PREFIX + 'meta/<string:eid>', methods= [ 'GET', 'PUT' ], defaults={'collection': 'packages'})
def element_get(eid, collection):
    el = db[collection].find_one({ 'id': eid })
    if el is None:
        abort(404)
    if request.method == 'PUT':
        # FIXME Do some sanity checks before storing
        if request.headers.get('content-type') == 'application/json':
            data = json.loads(request.data)
            if data['id'] != el['id']:
                abort(500)
            data['_id'] = el['_id']
            if collection == 'annotations':
                # Fix missing/wrong fields
                normalize_annotation(data)
            db[collection].save(clean_json(data))
            return make_response("Resource updated.", 201)
        abort(500)
    return current_app.response_class(json.dumps(el, indent=None if request.is_xhr else 2, cls=MongoEncoder),
                                      mimetype='application/json')


@app.route(API_PREFIX + 'user/', methods= [ 'GET' ])
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
        for res in aggr['result']:
            users.setdefault(res['_id'], {})[collection] = res['count']
    return current_app.response_class( json.dumps(users,
                                       indent=None if request.is_xhr else 2,
                                       cls=MongoEncoder),
                                       mimetype='application/json')

@app.route(API_PREFIX + 'user/<string:uid>/annotation', methods= [ 'GET' ])
def user_annotation_list(uid):
    return current_app.response_class( json.dumps(list(db['annotations'].find({'meta.dc:creator': uid})),
                                                  indent=None if request.is_xhr else 2,
                                                  cls=MongoEncoder),
                                       mimetype='application/json')

@app.route(API_PREFIX + 'package/', methods= [ 'GET', 'POST' ])
def package_list():
    if request.method == 'POST':
        # Insert a new package
        data = request.json

        # Mapping table for converted ids
        mapping = {}
        for m in data.get('medias', []):
            db['medias'].save(clean_json(m, mapping))
        for at in data.get('annotation-types', []):
            db['annotationtypes'].save(clean_json(at, mapping))
        for a in data.get('annotations', []):
            db['annotations'].save(clean_json(a, mapping))

        p = data['meta']

        # Interim hack for malformed data
        if p['main_media']['id-ref'] == 'package1':
            p['main_media']['id-ref'] = data['medias'][0]['id']

        newmediaref = mapping.get(p['main_media'].get('id-ref'))
        if newmediaref is not None:
            p['main_media']['id-ref'] = newmediaref

        fix_ids(p)
        # FIXME: there should be some way to specify associated media/annotationtypes/annotations.
        # Maybe store in meta some info containings ids?
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
    for atid in ats:
        at = db['annotationtypes'].find_one({ 'id': atid })
        if at is not None:
            p['annotation-types'].append(restore_json(at))
        else:
            print "Error: missing annotation type"
    return current_app.response_class(json.dumps(p, indent=None if request.is_xhr else 2, cls=MongoEncoder),
                                      mimetype='application/json')

# set the secret key.  keep this really secret:
app.secret_key = os.urandom(24)

if __name__ == "__main__":
    parser=OptionParser(usage="""Trace server.\n%prog [options]""")

    parser.add_option("-d", "--debug", dest="enable_debug", action="store_true",
                      help="Enable debug mode.", default=False)

    (options, args) = parser.parse_args()
    CONFIG.update(vars(options))

    if args and args[0] == 'shell':
        import pdb; pdb.set_trace()
        import sys; sys.exit(0)

    if CONFIG['enable_debug']:
        app.run(debug=True)
    else:
        app.run(debug=False)