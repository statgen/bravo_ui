#!/bin/sh

# Activate venv if present.
[ -d 'venv' ] && source 'venv/bin/activate'

export FLASK_ENV=development
export FLASK_APP=bravo_browser
export BRAVO_UI_CONFIG_FILE='config.py'
export BRAVO_UI_INSTANCE_DIR='./instance'

flask run --port 8089

# gunicorn -b 127.0.0.1:8089 -w 10 -k gevent "bravo_browser:create_app()"
