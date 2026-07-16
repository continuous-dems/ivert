# Implement the server's flask API.

from flask import Flask, jsonify

import jobs_database

app = Flask(__name__)

# Even though we're on the server, the API should only access query functions, therefore we're using the
# JobsDatabaseClient class
db_client = jobs_database.JobsDatabaseClient()


@app.get("/last_job")
def get_latest_job_number():
    last_jnum = db_client.fetch_latest_job_number_from_database()

    return jsonify(last_jnum)


@app.get("/jobs/<string:job_name>")
def get_job_info(job_name: str):
    # TODO: Implement

    return jsonify(job_status)


@app.get("/files/<string:job_name>")
def get_job_files(job_name: str):
    # TODO: Implement

    return jsonify(job_files)


@app.get("/ivert_version")
def get_ivert_version():
    # TODO: Implement
    return jsonify(__version__)


@app.get("/ivert_min_client_version")
def get_ivert_min_client_version():
    # TODO: Implement
    return jsonify(__version__)
