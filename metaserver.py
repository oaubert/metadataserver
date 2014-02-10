#! /usr/bin/python

#
# This file is part of MetaServer
#
# MetaServer is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# NoTS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with MetaServer.  If not, see <http://www.gnu.org/licenses/>.
#

import os
import json
import bson
import uuid
import time
from optparse import OptionParser
from flask import Flask, Response
from flask import session, request, redirect, url_for, current_app, abort, jsonify
import pymongo

# PARAMETRES
# DB = DataBase
# COL= Collection
DB   	  = 'mds'

API_PREFIX = '/api/'

# Server configuration
CONFIG = {
    # Enable debug. This implicitly disallows external access
    'enable_debug': False,
    # Run the server in external access mode (i.e. not only localhost)
    'allow_external_access': True,
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
    if len(data['id']) < 36:
        # It is not a UUID, generate one
        old = data['mds:oldid'] = data['id']
        mapping[old] = data['id'] = str(uuid.uuid1())
    return data
    
def clean_json(data, mapping=None):
    """Clean the input json.

    Mongo does not accept dots in attribute names.
    """
    # Fix ids for all elements
    fix_ids(data, mapping)
    # For media
    if 'http://advene.liris.cnrs.fr/ns/frame_of_reference/ms' in data:
        del data['http://advene.liris.cnrs.fr/ns/frame_of_reference/ms']
    # For any element
    # Author Metadata is in the meta dict, or in the dict itself
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
    # For any element
    meta = data.get('meta', {})
    for n in ('dc:created_contents', 'dc:creator_contents'):
        if n in meta:
            meta[n.replace('_', '.')] = meta[n]
            del meta[n]
    return data

@app.errorhandler(401)
def custom_401(error):
    return Response('Unauthorized access', 401, {'WWWAuthenticate':'Basic realm="Login Required"'})

@app.route("/")
def index():
    if 'userinfo' in session:
        #return 'Logged in as : %s' % escape(session['navigator'])
        #session['navigator']['id']="test";
        return "Logged in as " + session['userinfo']['id']
    return 'You are not logged in'

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

@app.route(API_PREFIX + 'annotation/', methods= [ 'GET', 'POST' ])
def annotation_list():
    if request.method == 'POST':
        # Insert a new annotation
        data = clean_json(request.json)
        # FIXME: what to do with media id, annotationtype id, creator (check for existence, or create by default?)
        db['annotations'].save(data)
        return jsonify(id=data['id'])
    else:
        querymap = { 'user': 'meta.dc:creator',
                     # 'media': 'media',
                     'type': 'meta.id-ref' }
        query = dict( (querymap.get(name, name), value)
                      for f in request.values.getlist('filter')
                      for name, value in f.split(':') )
        cursor = db['annotations'].find(query)
        response = current_app.response_class( json.dumps(list(restore_json(a) for a in cursor),
                                                          indent=None if request.is_xhr else 2,
                                                          cls=MongoEncoder),
                                               mimetype='application/json')
        return response

@app.route(API_PREFIX + 'annotation/<string:aid>', methods= [ 'GET' ])
def annotation_get(aid):
    a = db['annotations'].find_one({ 'id': aid })
    if a is None:
        abort(404)
    return current_app.response_class(json.dumps(a, indent=None if request.is_xhr else 2, cls=MongoEncoder), 
                                      mimetype='application/json')

@app.route(API_PREFIX + 'annotationtype/', methods= [ 'GET', 'POST' ])
def annotationtype_list():
    if request.method == 'POST':
        # Insert a new annotation type
        data = clean_json(request.json)
        # FIXME: what to do with media id, annotationtype id, creator (check for existence, or create by default?)
        db['annotationtypes'].save(data)
        return jsonify(id=data['id'])
    else:
        querymap = { 'user': 'meta.dc:creator' }
        query = dict( (querymap.get(name, name), value)
                      for f in request.values.getlist('filter')
                      for name, value in f.split(':') )
        cursor = db['annotationtypes'].find(query)
        response = current_app.response_class( json.dumps(list(restore_json(a) for a in cursor),
                                                          indent=None if request.is_xhr else 2,
                                                          cls=MongoEncoder),
                                               mimetype='application/json')
        return response

@app.route(API_PREFIX + 'annotationtype/<string:aid>', methods= [ 'GET' ])
def annotationtype_get(aid):
    a = db['annotationtypes'].find_one({ 'id': aid })
    if a is None:
        abort(404)
    return current_app.response_class(json.dumps(a, indent=None if request.is_xhr else 2, cls=MongoEncoder), 
                                      mimetype='application/json')

@app.route(API_PREFIX + 'media/', methods= [ 'GET', 'POST' ])
def media_list():
    if request.method == 'POST':
        # Insert a new media
        data = clean_json(request.json)
        db['medias'].save(data)
        return jsonify(id=data['id'])
    else:
        # FIXME: Specific /media filters: url:name, 
        querymap = { 'user': 'meta.dc:creator',
                     # 'url': 'url' 
                   }
        query = dict( (querymap.get(name, name), value)
                      for f in request.values.getlist('filter')
                      for name, value in f.split(':') )
        cursor = db['medias'].find(query)

        response = current_app.response_class( json.dumps(list(restore_json(m) for m in cursor),
                                                          indent=None if request.is_xhr else 2,
                                                          cls=MongoEncoder),
                                               mimetype='application/json')
        return response

@app.route(API_PREFIX + 'media/<string:mid>', methods= [ 'GET' ])
def media_get(mid):
    m = db['medias'].find_one({ 'id': mid })
    if m is None:
        abort(404)
    return current_app.response_class(json.dumps(m, indent=None if request.is_xhr else 2, cls=MongoEncoder),
                                      mimetype='application/json')

@app.route(API_PREFIX + 'user/', methods= [ 'GET' ])
def user_list():
    cursor = db['userinfo'].find()
    response = current_app.response_class( json.dumps(list(cursor),
                                                      indent=None if request.is_xhr else 2,
                                                      cls=MongoEncoder),
                                           mimetype='application/json')
    return response

@app.route(API_PREFIX + 'user/<string:uid>', methods= [ 'GET' ])
def user_get(uid):
    u = db['userinfo'].find_one({ 'id': uid })
    if u is None:
        abort(404)
    return current_app.response_class(json.dumps(u, indent=None if request.is_xhr else 2, cls=MongoEncoder),
                                      mimetype='application/json')

@app.route(API_PREFIX + 'user/<string:uid>/annotation', methods= [ 'GET' ])
def user_annotation_list(uid):
    return current_app.response_class( json.dumps(list(db['annotation'].find({'meta.dc:creator': uid})),
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
    mid = p['main_media']['id-ref']
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
                      help="Enable debug. This implicitly disallows external access.",
                      default=False)

    (options, args) = parser.parse_args()
    if options.enable_debug:
        options.allow_external_access = False
    CONFIG.update(vars(options))

    if args and args[0] == 'shell':
        import pdb; pdb.set_trace()
        import sys; sys.exit(0)

    if CONFIG['enable_debug']:
        app.run(debug=True)
    else:
        app.run(debug=False)
