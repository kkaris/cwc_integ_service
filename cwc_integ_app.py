import re
import time
import json
import docker
from os import path
from datetime import datetime
from flask import Flask, redirect, render_template, url_for, request, flash
from flask_wtf import Form
from flask_pymongo import PyMongo
from flask_bootstrap import Bootstrap
from wtforms import SubmitField

from get_logs import get_logs_for_container

import logging

logger = logging.getLogger('cwc-web-service')
logging.basicConfig(level=logging.DEBUG,
                    format='[%(asctime)s] %(name)s.%(levelname)s - %(funcName)s@%(lineno)s: %(message)s')

MAX_SESSIONS = 8
class SessionLimitExceeded(Exception):
    pass


app = Flask(__name__)
app.config["MONGO_URI"] = 'mongodb://localhost:27017/myDatabase'
app.config['SECRET_KEY'] = 'dev_key'
mongo = PyMongo(app)
Bootstrap(app)
MY_CONTAINER_LIST = 'cwc_service_containers.json'
TIME_FMT = '%Y%m%d%H%M%S'
DAY = 86400  # a day in seconds.


def _load_id_dict():
    if not path.exists(MY_CONTAINER_LIST):
        return {}
    id_dict = {}
    with open(MY_CONTAINER_LIST, 'r') as f:
        id_dict_strs = json.load(f)
    for id_val, date_str in id_dict_strs.items():
        id_dict[id_val] = datetime.strptime(date_str, TIME_FMT)
    return id_dict


def _dump_id_dict(id_dict):
    json_dict = {}
    for id_val, date in id_dict.items():
        json_dict[id_val] = date.strftime(TIME_FMT)
    with open(MY_CONTAINER_LIST, 'w') as f:
        json.dump(json_dict, f)
    return


def _record_my_container(cont_id, action):
    """Update the json containing the statuses of the containers."""
    assert action in ['add', 'remove'], "Invalid action: %s" % action
    id_dict = _load_id_dict()

    success = True
    if cont_id not in id_dict.keys():
        if action == 'add':
            logger.info("Adding %s to list of my containers." % cont_id)
            id_dict[cont_id] = datetime.utcnow()
        elif action == 'remove':
            logger.info("This container isn't mine or doesn't exist.")
            success = False
    else:
        if action == 'add':
            logger.info("This container was already registered.")
            success = False
        elif action == 'remove':
            date = id_dict.pop(cont_id)
            logger.info("Removing %s from list of my containers which was started "
                  "at %s." % (cont_id, date))
    if success:
        _dump_id_dict(id_dict)
    return success


def _check_timers():
    """Look through the containers and stop any timed-out containers."""
    # The logs are utc time, and this generally avoids any time-zone issues.
    # Won't work in python 2.
    now = datetime.utcnow()

    # This is not connected to the other dict instances deliberately. Do not
    # try to make the dict shared, because the below for-loop could then have
    # issues modifying an object while iterating over it.
    id_dict = _load_id_dict()
    logger.info("There are %d instances running." % len(id_dict))

    # Go through all the containers...
    client = docker.from_env()
    for cont_id, start_date in id_dict.items():
        logger.info("Examining %s" % cont_id)

        # Grab the date from the latest SPG log entry.
        cont = client.containers.get(cont_id)
        cont_logs = cont.logs()
        date_strings = re.findall('SPG:\s+;;\s+\[(.*?)\]', cont_logs.decode('utf-8'))
        if date_strings:
            latest_log_date = datetime.strptime(date_strings[-1], '%m/%d/%Y %H:%M:%S')
        else:
            logger.info("WARNING: Did not find any date strings in container logs "
                  "for %s." % cont_id)
            latest_log_date = start_date

        # Check both whether the logs have been silent for more than a day
        # (neglect) or whether the session has been running for more than 5
        # days (hogging).
        log_stalled = (now - latest_log_date).seconds
        total_dur = (now - start_date).seconds
        if log_stalled > 3600:
            logger.info("Container %s timed out after %d seconds of empty logs."
                  % (cont_id, log_stalled))
            _stop_container(cont_id)
        elif total_dur > DAY/2:
            logger.info("Container %s timed out after %d seconds of running."
                  % (cont_id, total_dur))
            _stop_container(cont_id)
    return


def get_increment_port():
    port_json = mongo.db.ports.find_one()
    if port_json is None:
        port = 8000
        mongo.db.ports.insert_one({'port': port})
    else:
        port = port_json['port']
        mongo.db.ports.update_one({'port': port}, {'$set': {'port': port + 1}})
        port += 1
    return port


def get_num_sessions():
    sessions_json = mongo.db.sessions.find_one()
    if not sessions_json:
        return 0
    num_sessions = sessions_json['num_sessions']
    return num_sessions


def increment_sessions():
    sessions_json = mongo.db.sessions.find_one()
    if sessions_json is None:
        reset_sessions()
    num_sessions = sessions_json['num_sessions']
    if num_sessions == MAX_SESSIONS:
        raise SessionLimitExceeded()
    mongo.db.sessions.update_one({'num_sessions': num_sessions},
                                 {'$set': {'num_sessions': num_sessions + 1}})
    return num_sessions + 1


def decrement_sessions():
    sessions_json = mongo.db.sessions.find_one()
    if sessions_json is None:
        reset_sessions()
    num_sessions = sessions_json['num_sessions']
    mongo.db.sessions.update_one({'num_sessions': num_sessions},
                                 {'$set': {'num_sessions': num_sessions - 1}})
    return num_sessions - 1


def add_token(token):
    mongo.db.tokens.insert_one({'token': token})


def has_token(token):
    tokens = mongo.db.tokens.find()
    if tokens is None:
        return False
    for tok in tokens:
        if tok['token'] == token:
            return True
    return False


def _launch_app(interface_port_num, app_name, extension=''):
    num_sessions = get_num_sessions()
    if num_sessions >= MAX_SESSIONS:
        logger.info('Number of sessions: %d' % num_sessions)
        # TODO: this should be part of the index page with buttons
        # greyed out
        return 'There are currently too many sessions, please come back later.'
    # Here we check if the same token was already used to start a session
    token = request.form['csrf_token']
    if has_token(token):
        # Flash could be nice but it gets placed on the home page instead of
        # the page with the dialogue for some reason
        # flash('You already have a session!')
        return ('', 204)
        #return 'You already have a running session, please stop it ' + \
        #    'and refresh the main page again to start another one.'
    # We add the token to make sure it can't be reused
    add_token(token)
    port = get_increment_port()
    base_host = 'http://' + str(request.host).split(':')[0]
    host = base_host + (':%d' % port + extension)
    logger.info('Will redirect to address: %s' % host)
    cont_id = _run_container(port, interface_port_num)
    logger.info('Start redirecting %s interface.' % app_name)
    return render_template('launch_dialogue.html', dialogue_url=host,
                           manager_url=base_host, container_id=cont_id,
                           time_out=90)


class ClicForm(Form):
    submit_button = SubmitField('Launch with CLiC')


class SbgnForm(Form):
    submit_button = SubmitField('Launch with SBGN')


@app.route('/')
def hello():
    clic_form = ClicForm()
    sbgn_form = SbgnForm()
    kwargs = {'clic_form': clic_form, 'sbgn_form': sbgn_form}
    return render_template('index.html', **kwargs)


@app.route('/launch_clic', methods=['POST'])
def launch_clic():
    return _launch_app(8000, 'CLIC', '/clic/bio')


@app.route('/launch_sbgn', methods=['POST'])
def launch_sbgn():
    return _launch_app(3000, 'SBGN')


@app.route('/end_session/<cont_id>', methods=['DELETE'])
def stop_session(cont_id):
    logger.info("Request to end %s." % cont_id)
    assert cont_id, "Bad request. Need an id."
    _stop_container(cont_id)
    return 'Success!', 200


def _stop_container(cont_id, remove_record=True):
    if remove_record:
        assert _record_my_container(cont_id, 'remove'), \
            "Could not remove container because it is not my own."
    client = docker.from_env()
    cont = client.containers.get(cont_id)
    logger.info("Got container %s, aka %s." % (cont.id, cont.name))
    get_logs_for_container(cont)
    cont.stop()
    cont.remove()
    logger.info("Container removed.")
    decrement_sessions()
    return


def _run_container(port, expose_port):
    num_sessions = increment_sessions()
    logger.info('We now have %d active sessions' % num_sessions)
    client = docker.from_env()
    cont = client.containers.run('cwc-integ:latest',
                                 '/sw/cwc-integ/startup.sh',
                                 detach=True,
                                 ports={('%d/tcp' % expose_port): port})
    logger.info('Launched container %s exposing port %d via port %d' %
          (cont, expose_port, port))
    _record_my_container(cont.id, 'add')
    return cont.id


def reset_sessions():
    """Reset all the db sessions."""
    logger.info('Resetting sessions')
    sessions_json = mongo.db.sessions.find_one()
    if sessions_json is None:
        mongo.db.sessions.insert_one({'num_sessions': 0})
    else:
        mongo.db.sessions.update_one({}, {'$set': {'num_sessions': 0}})


def cleanup():
    """Stop all the currently running containers and remove."""
    logger.info("Starting cleanup.")
    print("+" + "-"*78 + "+")
    print("| %-76s |" % "Grabbing logs, stopping, and removing all docker containers...")
    print("| %-76s |" % "Please wait, as this may take a while.")
    print("+" + "-"*78 + "+")
    id_dict = _load_id_dict()
    num_conts = len(id_dict)
    for i, (cont_id, start_date) in enumerate(id_dict.items()):
        try:
            print("(%d/%d) Resolving %s...." % (i+1, num_conts, cont_id))
            _stop_container(cont_id)
        except Exception as e:
            logger.error("Faild to shut down the container: %s!" % (cont_id))
            logger.error("Reasion:")
            logger.exception(e)
            logger.info("Continuing...")
    print("+" + "-"*78 + "+")
    print("| %-76s |" % "All done! Have a nice day! :)")
    print("+" + "-"*78 + "+")


def monitor():
    """Check session timers and clean up old session periodically."""
    logger.info("Monitor starting.")
    try:
        while True:
            time.sleep(60*15)  # every 15 minutes
            logger.info("Checking session in monitor...")
            _check_timers()
    except BaseException as e:
        logger.info("Monitor is closing with:")
        logger.exception(e)
    return


if __name__ == '__main__':
    from sys import argv
    logger.setLevel(logging.INFO)
    if argv[1] == 'cleanup':
        cleanup()
    elif argv[1] == 'monitor':
        monitor()
    elif argv[1] == 'reset':
        reset_sessions()
    else:
        guni_logger = logging.getLogger('gunicorn.error')
        logger.handlers.extend(guni_logger.handlers)
        logger.setLevel(logging.INFO)
        logger.info("Logging is working!")
        app.run(host='0.0.0.0')
