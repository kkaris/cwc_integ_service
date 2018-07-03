import docker
from flask import Flask, redirect
from flask_pymongo import PyMongo


app = Flask(__name__)
app.config["MONGO_URI"] = "mongodb://localhost:27017/myDatabase"
mongo = PyMongo(app)


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


@app.route('/')
def hello():
    return 'Bob with Bioagents'


@app.route('/launch')
def launch():
    port = get_increment_port()
    client = docker.from_env()
    cont = client.containers.run('cwc-integ:latest',
                                 '/sw/cwc-integ/startup.sh',
                                 detach=True, ports={'8000/tcp': port})
    print(cont)
    return redirect("http://localhost:%d" % port)


if __name__ == '__main__':
    app.run()
