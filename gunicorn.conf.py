import bulk_csv_patch


def post_fork(server, worker):
    bulk_csv_patch.install(worker.app.wsgi())
