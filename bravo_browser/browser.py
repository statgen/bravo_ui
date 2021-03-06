from flask import (
    current_app, Blueprint, request, jsonify, make_response, Response, abort, render_template,
    redirect, url_for, session, send_file, stream_with_context, send_from_directory)
from flask_cors import CORS
from flask_compress import Compress
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
import google_auth_oauthlib.flow
import functools
import requests
from webargs import fields
from webargs.flaskparser import use_kwargs, use_args
from datetime import timedelta
import re
import json
import urllib.parse
from bravo_browser.models import users, feedbacks

bp = Blueprint('browser', __name__, template_folder='templates', static_folder='static')
CORS(bp)

compress = Compress()
login_manager = LoginManager()


class User(UserMixin):
    pass


@login_manager.user_loader
def user_loader(email):
    document = users.load(email)
    if document is None:
        return None
    user = User()
    user.id = document['user_id']
    user.picture = document['picture']
    user.agreed_to_terms = document['agreed_to_terms']
    return user


def get_authorization_url():
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file( current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'],
       scopes=['openid', 'https://www.googleapis.com/auth/userinfo.email'])
    flow.redirect_uri = url_for('.oauth2callback', _external = True, _scheme = 'https')
    return flow.authorization_url(access_type = 'offline', include_granted_scopes = 'true')


def require_authorization(func):
    @functools.wraps(func)
    def authorization_wrapper(*args, **kwargs):
        if current_app.config['GOOGLE_OAUTH_CLIENT_SECRET']:
            if current_user.is_anonymous:
                authorization_url, state = get_authorization_url()
                session['state'] = state
                session['original_request_path'] = request.path
                return redirect(authorization_url)
            if not hasattr(current_user, 'agreed_to_terms') or not current_user.agreed_to_terms:
                session['original_request_path'] = request.path
                return redirect(url_for('.terms'))
        return func(*args, **kwargs)
    return authorization_wrapper


@bp.route('/favicon')
@bp.route('/favicon.ico')
def favicon():
    return send_from_directory(bp.static_folder, 'favicon.ico', mimetype='image/vnd.microsoft.icon')



@bp.route('/oauth2callback', methods = ['GET', 'POST'])
def oauth2callback():
    state = session['state']
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
       current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'],
       scopes = ['openid', 'https://www.googleapis.com/auth/userinfo.email'],
       state = state)
    flow.redirect_uri = url_for('.oauth2callback', _external = True, _scheme = 'https')
    if current_app.config['PROXY'] and not request.url.startswith('https'): # current version of Google OAuth python library doesn't handle situation when behind HTTPS proxy server
        protocol = request.headers.get('X-Forwarded-Proto', '')
        if protocol == 'https':
            authorization_response = re.sub(r'^http:', 'https:', request.url)
    else:
        authorization_response = request.url
    flow.fetch_token(authorization_response = authorization_response)
    credentials = flow.credentials

    response = requests.get('https://accounts.google.com/.well-known/openid-configuration')
    response.raise_for_status()
    openid_endpoints = json.loads(response.text)
    userinfo_endpoint = openid_endpoints['userinfo_endpoint']

    response = requests.get(userinfo_endpoint, headers = { 'Authorization': f'Bearer {credentials.token}' })
    response.raise_for_status()
    userinfo = json.loads(response.text)

    if not users.in_whitelist(userinfo['email']):
        abort(403)

    document = users.load(userinfo['email'])
    if document is None:
        document = users.save(userinfo['email'], userinfo['picture'])

    user = User()
    user.id = userinfo['email']
    user.picture = userinfo['picture']

    if user.picture != document['picture']:
        users.update_picture(user.id, user.picture)

    login_user(user, remember = True, duration = timedelta(days = 1))
    return redirect(session.pop('original_request_path', url_for('.home')))


@bp.route('/about', methods = ['GET'])
def about():
    return render_template('about.html', show_brand = True, show_signin = current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'] != '')


@bp.route('/terms', methods = ['GET'])
def terms():
    return render_template('terms.html', show_brand = True, show_signin = current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'] != '')


@bp.route('/downloads', methods = ['GET'])
@require_authorization
def downloads():
    return render_template('downloads.html', show_brand = True, show_signin = current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'] != '')


@bp.route('/downloads/vcf/<string:chromosome>', methods = ['GET'])
@require_authorization
def download_vcf(chromosome):
    if not chromosome in current_app.config['DOWNLOAD_CHROMOSOMES_VCF']:
        abort(404)
    return make_response(send_file(current_app.config['DOWNLOAD_CHROMOSOMES_VCF'][chromosome][1], as_attachment = True, mimetype='application/gzip'))


@bp.route('/downloads/coverage/<string:chromosome>', methods = ['GET'])
@require_authorization
def download_coverage(chromosome):
    if not chromosome in current_app.config['DOWNLOAD_CHROMOSOMES_COVERAGE']:
        abort(404)
    return make_response(send_file(current_app.config['DOWNLOAD_CHROMOSOMES_COVERAGE'][chromosome][1], as_attachment = True, mimetype='application/gzip'))


@bp.route('/agree_to_terms', methods = ['GET'])
def agree_to_terms():
    if current_app.config['GOOGLE_OAUTH_CLIENT_SECRET']:
        if not current_user.is_anonymous:
            current_user.agreed_to_terms = True
            users.update_agreed_to_terms(current_user.id, current_user.agreed_to_terms)
            return redirect(session.pop('original_request_path', url_for('.home')))
    abort(404)


@bp.route('/signin', methods = ['GET'])
def signin():
    if current_app.config['GOOGLE_OAUTH_CLIENT_SECRET']:
        authorization_url, state = get_authorization_url()
        session['state'] = state
        return redirect(authorization_url)
    abort(404)


@bp.route('/logout', methods = ['GET'])
def logout():
    if current_app.config['GOOGLE_OAUTH_CLIENT_SECRET']:
        logout_user()
        return redirect(url_for('.home'))
    abort(404)


@bp.route('/', methods = ['GET'])
def home():
    return render_template('home.html', show_brand = False, show_signin = current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'] != '')


@bp.route('/autocomplete', methods = ['GET'])
def autocomplete():
    query = request.args.get('query', '')
    suggestions = []
    if query:
        api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/genes?name={query}")
        if api_response.status_code == 200:
            payload = api_response.json()
            if not payload['error']:
                for gene in payload['data']:
                    suggestions.append({
                       'value': gene['gene_name'],
                       'data': {
                          'feature': 'gene',
                          'chrom': gene['chrom'],
                          'start': gene['start'],
                          'stop': gene['stop'],
                          'type': gene['gene_type']
                       }
                    })
        if len(suggestions) < 10 and query.startswith('rs'):
            api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/snv?variant_id={query}")
            if api_response.status_code == 200:
                payload = api_response.json()
                if not payload['error']:
                    for variant in payload['data']:
                        suggestions.append({
                           'value': [ x for x in variant['rsids'] if x.startswith(query) ][0],
                           'data': {
                              'feature': 'snv',
                              'variant_id': variant['variant_id'],
                              'type': variant['annotation']['region']['consequence'][0]
                           }
                        })
    return make_response(jsonify({ "suggestions": suggestions }), 200)


_regex_pattern_chr = r'^(?:CHR)?(\d+|X|Y|M|MT)'
_regex_pattern_chr_pos = _regex_pattern_chr + r'\s*[-:/]\s*([\d,]+)'
_regex_pattern_chr_start_end = _regex_pattern_chr_pos + r'\s*[-:/]\s*([\d,]+)'
_regex_pattern_chr_pos_ref_alt = _regex_pattern_chr_pos + r'\s*[-:/]\s*([ATCG]+)\s*[-:/]\s*([ATCG]+)'
_regex_pattern_rsid = r'^(?:rs)(\d+)'

_regex_chr = re.compile(_regex_pattern_chr+'$', re.IGNORECASE)
_regex_chr_pos = re.compile(_regex_pattern_chr_pos+'$', re.IGNORECASE)
_regex_chr_start_end = re.compile(_regex_pattern_chr_start_end+'$', re.IGNORECASE)
_regex_chr_pos_ref_alt = re.compile(_regex_pattern_chr_pos_ref_alt+'$', re.IGNORECASE)
_regex_rsid = re.compile(_regex_pattern_rsid+'$')


search_argmap = {
    'value': fields.Str(required = True, validate = lambda x: len(x) > 0,
                        error_messages = {'validator_failed': 'Value must be a non-empty string.'}),
    'chrom': fields.Str(required = False, validate = lambda x: len(x) > 0,
                        error_messages = {'validator_failed': 'Value must be a non-empty string.'}),
    'pos': fields.Int(required = False, validate = lambda x: x > 0,
                      error_messages = {'validator_failed': 'Value must be greater than 0.'}),
    'start': fields.Int(required = False, validate = lambda x: x > 0,
                        error_messages = {'validator_failed': 'Value must be greater than 0.'}),
    'stop': fields.Int(required = False, validate = lambda x: x > 0,
                       error_messages = {'validator_failed': 'Value must be greater than 0.'}),
    'ref': fields.Str(required = False, validate = lambda x: len(x) > 0,
                      error_messages = {'validator_failed': 'Value must be a non-empty string.'}),
    'alt': fields.Str(required = False, validate = lambda x: len(x) > 0,
                      error_messages = {'validator_failed': 'Value myst be a non-empty string.'})
}


@bp.route('/search', methods = ['GET'])
@use_args(search_argmap, location='query')
def search(args):
    if 'chrom' in args and 'start' in args and 'stop' in args: # suggested gene name
        args = {
           'variants_type': 'snv',
           'gene_name': args['value']
        }
        return redirect(url_for('.gene_page', **args))
    elif 'chrom' in args and 'pos' in args and 'ref' in args and 'alt' in args: # suggested snv
        args = {
          'variant_type': 'snv',
          'variant_id': f"{args['chrom']}-{args['pos']}-{args['ref']}-{args['alt']}"
        }
        return redirect(url_for('.variant_page', **args))
    else:  # typed value
        match = _regex_chr_start_end.match(args['value'])
        if match is not None:
            args = {
               'variants_type': 'snv',
               'chrom': match.groups()[0],
               'start': match.groups()[1],
               'stop': match.groups()[2]}
            return redirect(url_for('.region_page', **args))
        else:
            match = _regex_chr_pos_ref_alt.match(args['value'])
            if match is not None:
                variant_id = f'{match.groups()[0]}-{match.groups()[1]}-{match.groups()[2]}-{match.groups()[3]}'.upper()
                if variant_id.startswith('CHR'):
                    variant_id = variant_id[3:]
                api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/snv?variant_id={variant_id}")
                if api_response.status_code == 200:
                    payload = api_response.json()
                    if not payload['error']:
                        for variant in payload['data']:
                            if variant['variant_id'] == variant_id:
                                args = {
                                   'variant_type': 'snv',
                                   'variant_id': variant['variant_id']
                                }
                                return redirect(url_for('.variant_page', **args))
            else:
                match = _regex_rsid.match(args['value'])
                if match is not None:
                    api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/snv?variant_id={args['value']}")
                    if api_response.status_code == 200:
                        payload = api_response.json()
                        if not payload['error']:
                            for variant in payload['data']:
                                if any(rsid == args['value'] for rsid in variant['rsids']):
                                    args = {
                                       'variant_type': 'snv',
                                       'variant_id': variant['variant_id']
                                    }
                                    return redirect(url_for('.variant_page', **args))
                else:
                    api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/genes?name={args['value']}")
                    if api_response.status_code == 200:
                        payload = api_response.json()
                        if not payload['error']:
                            for gene in payload['data']:
                                if gene['gene_name'].upper() == args['value'].upper():
                                    args = {
                                       'variants_type': 'snv',
                                       'gene_name': args['value'].upper()
                                    }
                                    return redirect(url_for('.gene_page', **args))
    return redirect(url_for('.not_found', message = f'We coudn\'t find what you wanted.'))


@bp.route('/feedback', methods = ['POST'])
@require_authorization
def feedback():
    feedbacks.save(current_user.id, request.form['page-url'], request.form['message-text'])
    return Response(json.dumps({}), status=200, mimetype='application/json')


@bp.route('/not_found/<message>', methods = ['GET'])
@require_authorization
def not_found(message):
    return render_template('not_found.html', show_brand = True, message = message), 404


@bp.route('/region/<string:variants_type>/<string:chrom>-<int:start>-<int:stop>')
@require_authorization
def region_page(variants_type, chrom, start, stop):
    if variants_type not in ['snv', 'sv']:
        return not_found(f'We couldn\'t find what you wanted')
    return render_template('region.html', show_brand = True, show_signin = current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'] != '', variants_type = variants_type, chrom = str(chrom), start = start, stop = stop)


@bp.route('/gene/<string:variants_type>/<string:gene_name>')
@require_authorization
def gene_page(variants_type, gene_name):
    if variants_type not in ['snv', 'sv']:
        return not_found(f'We couldn\'t find what you wanted')
    return render_template('gene.html', show_brand = True, show_signin = current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'] != '', variants_type = variants_type, gene_name = gene_name)


@bp.route('/variant/<string:variant_type>/<string:variant_id>')
@require_authorization
def variant_page(variant_type, variant_id):
    if variant_type not in ['snv', 'sv']:
        return not_found(f'We couldn\'t find what you wanted')
    return render_template('variant.html', show_brand = True, show_signin = current_app.config['GOOGLE_OAUTH_CLIENT_SECRET'] != '', variant_id = variant_id)


variant_argmap = {
    'variant_id': fields.Str(location = 'view_args', required = True, validate = lambda x: len(x) > 0, error_messages = {'validator_failed': 'Value must be a non-empty string.'})
}


@bp.route('/variant/api/snv/<string:variant_id>')
@require_authorization
@use_kwargs(variant_argmap, location='view_args')
def variant(variant_id):
    api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/snv?variant_id={variant_id}&full=1", headers = { 'Accept-Encoding': 'gzip' })
    if api_response.status_code == 200:
        return make_response(api_response.content, 200)
    return not_found(f'I couldn\'t find what you wanted')


@bp.route('/variant/api/snv/cram/summary/<string:variant_id>')
@require_authorization
@use_kwargs(variant_argmap, location='view_args')
def variant_cram_info(variant_id):
    api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/sequence/summary?variant_id={variant_id}")
    if api_response.status_code == 200:
        return make_response(api_response.content, 200)
    return not_found(f'I couldn\'t find what you wanted')


variant_cram_argmap = {
    'variant_id': fields.Str(location = 'view_args', required = True, validate = lambda x: len(x) > 0, error_messages = {'validator_failed': 'Value must be a non-empty string.'}),
    'sample_het': fields.Bool(location = 'view_args', required = True),
    'sample_no': fields.Int(location = 'view_args', required = True, validate = lambda x: x > 0, error_messages = {'validator_failed': 'Value must be greater than 0.'})
}


@bp.route('/variant/api/snv/cram/<string:variant_id>-<int:sample_het>-<int:sample_no>')
@require_authorization
@use_kwargs(variant_cram_argmap, location='view_args')
def variant_cram(variant_id, sample_het, sample_no):
    request_str = (f"{current_app.config['BRAVO_API_URI']}/sequence?variant_id={variant_id}"
                   f"&sample_no={sample_no}&heterozygous={sample_het}&index=0")
    api_response = requests.get(request_str, headers = {'Range': request.headers['Range']}, stream = True)
    return Response(
       stream_with_context(api_response.iter_content(chunk_size = 1024)),
       status = api_response.status_code,
       content_type = api_response.headers['Content-Type'],
       headers = { 'Content-Range': api_response.headers['Content-Range'], 'Content-Length': api_response.headers['Content-Length']},
       direct_passthrough = True
    )


@bp.route('/variant/api/snv/crai/<string:variant_id>-<int:sample_het>-<int:sample_no>')
@require_authorization
@use_kwargs(variant_cram_argmap, location='view_args')
def variant_crai(variant_id, sample_het, sample_no):
    request_str = (f"{current_app.config['BRAVO_API_URI']}/sequence?"
                   f"variant_id={variant_id}&sample_no={sample_no}&heterozygous={sample_het}"
                   f"&index=1")
    api_response = requests.get(request_str, stream = True)
    return Response(
       stream_with_context(api_response.iter_content(chunk_size = 1024)),
       status = api_response.status_code,
       content_type = api_response.headers['Content-Type'],
       headers = { 'Content-Length': api_response.headers['Content-Length']}
    )

@bp.route('/qc/api')
@require_authorization
def qc():
    api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/qc", headers = { 'Accept-Encoding': 'gzip' })
    if api_response.status_code == 200:
        return make_response(api_response.content, 200)
    return not_found(f'I couldn\'t find what you wanted')


genes_argmap = {
    'chrom': fields.Str(location = 'view_args', required = True, validate = lambda x: len(x) > 0, error_messages = {'validator_failed': 'Value must be a non-empty string.'}),
    'start': fields.Int(location = 'view_args', required = True, validate = lambda x: x > 0, error_messages = {'validator_failed': 'Value must be greater than 0.'}),
    'stop': fields.Int(location = 'view_args', required = True, validate = lambda x: x > 0, error_messages = {'validator_failed': 'Value must be greater than 0.'})
}


@bp.route('/genes/<string:chrom>-<int:start>-<int:stop>')
@require_authorization
@use_kwargs(genes_argmap, location='view_args')
def genes(chrom, start, stop):
    api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/genes?chrom={chrom}&start={start}&stop={stop}&full=1", headers = { 'Accept-Encoding': 'gzip' })
    if api_response.status_code == 200:
        return make_response(api_response.content, 200)
    return not_found(f'I couldn\'t find what you wanted')


genes_name_argmap = {
    'name': fields.Str(location = 'view_args', required = True, validate = lambda x: len(x) > 0, error_messages = {'validator_failed': 'Value must be a non-empty string.'}),
}
@bp.route('/genes/api/<string:name>')
@require_authorization
@use_kwargs(genes_name_argmap, location='view_args')
def genes_by_name(name):
    api_response = requests.get(f"{current_app.config['BRAVO_API_URI']}/genes?name={name}&full=1", headers = { 'Accept-Encoding': 'gzip' })
    if api_response.status_code == 200:
        return make_response(api_response.content, 200)
    return not_found(f'I couldn\'t find what you wanted')


coverage_route_argmap = {
    'chrom': fields.Str(location = 'view_args', required = True, validate = lambda x: len(x) > 0, error_messages = {'validator_failed': 'Value must be a non-empty string.'}),
    'start': fields.Int(location = 'view_args', required = True, validate = lambda x: x > 0, error_messages = {'validator_failed': 'Value must be greater than 0.'}),
    'stop': fields.Int(location = 'view_args', required = True, validate = lambda x: x > 0, error_messages = {'validator_failed': 'Value must be greater than 0.'})
}

coverage_json_argmap = {
    'size': fields.Int(location = 'json', required = True, validate = lambda x: x > 0, error_messages = {'validation_failed': 'Value must be greater then 0'}),
    'next': fields.Str(location = 'json', required = True, allow_none = True, validate = lambda x: len(x) > 0, error_message = {'validator_failed': 'Value must be a non-empty string.'})
}


@bp.route('/coverage/<string:chrom>-<int:start>-<int:stop>', methods = ['POST'])
@require_authorization
@use_kwargs(coverage_route_argmap, location='view_args')
@use_kwargs(coverage_json_argmap, location='json')
def coverage(chrom, start, stop, size, next):
    if next is not None:
        url = f"{current_app.config['BRAVO_API_URI']}{next}"
    else:
        url = f"{current_app.config['BRAVO_API_URI']}/coverage?chrom={chrom}&start={start}&stop={stop}&limit={size}"
    api_response = requests.get(url, headers = { 'Accept-Encoding': 'gzip' })
    if api_response.status_code == 200:
        payload = api_response.json()
        if not payload['error'] and payload['next'] is not None:
            url = urllib.parse.urlparse(payload['next'])
            url = url._replace(scheme = '', netloc = '')
            payload['next'] = urllib.parse.urlunparse(url)
        response = make_response(jsonify(payload), 200)
        response.mimetype = 'application/json'
        return response
    return not_found(f'I coudn\'t find what you wanted')


@bp.route('/variants/<string:variants_type>', methods = ['POST', 'GET'])
@require_authorization
def variants_meta(variants_type):
    url = f"{current_app.config['BRAVO_API_URI']}/{variants_type}/filters"
    api_response = requests.get(url)
    if api_response.status_code == 200:
        payload = api_response.json()
        return make_response(jsonify(payload), 200)
    return render_template('not_found.html', show_brand = True, message = "Bad query!"), 404


@bp.route('/variants/region/<string:variants_type>/<string:chrom>-<int:start>-<int:stop>/histogram', methods = ['POST', 'GET'])
@require_authorization
def region_variants_histogram(variants_type, chrom, start, stop):
    sort = []
    args = []
    size = None
    url = None

    filter_type = {
       '=': 'eq',
       '!=': 'ne',
       '<': 'lt',
       '>': 'gt',
       '<=': 'lte',
       '>=': 'gte'
    }

    if request.method == 'POST':
        params = request.get_json()
        if params:
            for f in params.get('filters', []):
                if isinstance(f, list):
                    if len(f) > 0 and len(set( x["field"] for x in f )) == 1:
                        args.append('{}={}'.format(f[0]['field'], ','.join(f'{filter_type.get(x["type"], "eq")}:{x["value"]}' for x in f)))
                else:
                    args.append(f'{f["field"]}={filter_type.get(f["type"], "eq")}:{f["value"]}')
            if 'windows' in params:
                args.append(f'windows={params["windows"]}')

    url = f"{current_app.config['BRAVO_API_URI']}/region/{variants_type}/histogram?chrom={chrom}&start={start}&stop={stop}"
    if args:
        url += f"&{'&'.join(args)}"

    print(url)

    api_response = requests.get(url)
    if api_response.status_code == 200:
        payload = api_response.json()
        return make_response(jsonify(payload), 200)
    return render_template('not_found.html', show_brand = True, message = "Bad query!"), 404


@bp.route('/variants/region/<string:variants_type>/<string:chrom>-<int:start>-<int:stop>/summary', methods = ['POST', 'GET'])
@require_authorization
def region_variants_summary(variants_type, chrom, start, stop):
    args = []
    url = None

    filter_type = {
       '=': 'eq',
       '!=': 'ne',
       '<': 'lt',
       '>': 'gt',
       '<=': 'lte',
       '>=': 'gte'
    }

    if request.method == 'POST':
        params = request.get_json()
        if params:
            for f in params.get('filters', []):
                if isinstance(f, list):
                    if len(f) > 0 and len(set( x["field"] for x in f )) == 1:
                        args.append('{}={}'.format(f[0]['field'], ','.join(f'{filter_type.get(x["type"], "eq")}:{x["value"]}' for x in f)))
                else:
                    args.append(f'{f["field"]}={filter_type.get(f["type"], "eq")}:{f["value"]}')

    url = f"{current_app.config['BRAVO_API_URI']}/region/{variants_type}/summary?chrom={chrom}&start={start}&stop={stop}"
    if args:
        url += f"&{'&'.join(args)}"

    api_response = requests.get(url)
    if api_response.status_code == 200:
        payload = api_response.json()
        return make_response(jsonify(payload), 200)
    return render_template('not_found.html', show_brand = True, message = "Bad query!"), 404


@bp.route('/variants/gene/<string:variants_type>/<string:gene_name>/summary', methods = ['POST', 'GET'])
@require_authorization
def gene_variants_summary(variants_type, gene_name):
    args = []
    url = None

    filter_type = {
       '=': 'eq',
       '!=': 'ne',
       '<': 'lt',
       '>': 'gt',
       '<=': 'lte',
       '>=': 'gte'
    }

    if request.method == 'POST':
        params = request.get_json()
        if params:
            # print('gene summary params = ', params)
            for f in params.get('filters', []):
                if isinstance(f, list):
                    if len(f) > 0 and len(set( x["field"] for x in f )) == 1:
                        args.append('{}={}'.format(f[0]['field'], ','.join(f'{filter_type.get(x["type"], "eq")}:{x["value"]}' for x in f)))
                else:
                    args.append(f'{f["field"]}={filter_type.get(f["type"], "eq")}:{f["value"]}')
            if 'introns' in params:
                args.append(f'introns={params["introns"]}')

    url = f"{current_app.config['BRAVO_API_URI']}/gene/{variants_type}/summary?name={gene_name}"
    if args:
        url += f"&{'&'.join(args)}"

    api_response = requests.get(url)
    if api_response.status_code == 200:
        payload = api_response.json()
        return make_response(jsonify(payload), 200)
    return render_template('not_found.html', show_brand = True, message = "Bad query!"), 404


@bp.route('/variants/gene/<string:variants_type>/<string:gene_name>/histogram', methods = ['POST', 'GET'])
@require_authorization
def gene_variants_histogram(variants_type, gene_name):
    sort = []
    args = []
    size = None
    url = None

    filter_type = {
       '=': 'eq',
       '!=': 'ne',
       '<': 'lt',
       '>': 'gt',
       '<=': 'lte',
       '>=': 'gte'
    }

    if request.method == 'POST':
        params = request.get_json()
        if params:
            print('gene histogram params = ', params)
            for f in params.get('filters', []):
                if isinstance(f, list):
                    if len(f) > 0 and len(set( x["field"] for x in f )) == 1:
                        args.append('{}={}'.format(f[0]['field'], ','.join(f'{filter_type.get(x["type"], "eq")}:{x["value"]}' for x in f)))
                else:
                    args.append(f'{f["field"]}={filter_type.get(f["type"], "eq")}:{f["value"]}')
            if 'windows' in params:
                args.append(f'windows={params["windows"]}')
            if 'introns' in params:
                args.append(f'introns={params["introns"]}')

    url = f"{current_app.config['BRAVO_API_URI']}/gene/{variants_type}/histogram?name={gene_name}"
    if args:
        url += f"&{'&'.join(args)}"

    print(url)

    api_response = requests.get(url)
    if api_response.status_code == 200:
        payload = api_response.json()
        return make_response(jsonify(payload), 200)
    return render_template('not_found.html', show_brand = True, message = "Bad query!"), 404


@bp.route('/variants/region/<string:variants_type>/<string:chrom>-<int:start>-<int:stop>', methods = ['POST', 'GET'])
@require_authorization
def variants(variants_type, chrom, start, stop):
    sort = []
    args = []
    size = None
    url = None

    filter_type = {
       '=': 'eq',
       '!=': 'ne',
       '<': 'lt',
       '>': 'gt',
       '<=': 'lte',
       '>=': 'gte'
    }

    if request.method == 'POST':
        params = request.get_json()
        if params:
            if 'next' in params and params['next'] is not None:
                url = params['next']
            if 'size' in params:
                size = int(params['size'])
            for f in params.get('filters', []):
                if isinstance(f, list):
                    if len(f) > 0 and len(set( x["field"] for x in f )) == 1:
                        args.append('{}={}'.format(f[0]['field'], ','.join(f'{filter_type.get(x["type"], "eq")}:{x["value"]}' for x in f)))
                else:
                    args.append(f'{f["field"]}={filter_type.get(f["type"], "eq")}:{f["value"]}')
            for s in params.get('sorters', []):
                sort.append(f'{s["field"]}:{s["dir"]}')

    if url is not None:
        url = f"{current_app.config['BRAVO_API_URI']}{url}"
    else:
        url = f"{current_app.config['BRAVO_API_URI']}/region/{variants_type}?chrom={chrom}&start={start}&stop={stop}"
        if size:
            url += f'&limit={size}'
        if args:
            url += f"&{'&'.join(args)}"
        if sort:
            url += f"&sort={','.join(sort)}"

    print(url)

    api_response = requests.get(url)
    if api_response.status_code == 200:
        payload = api_response.json()
        if not payload['error'] and payload['next'] is not None:
            # remove host url because we don't want to expose what is our api endpoint
            url = urllib.parse.urlparse(payload['next'])
            url = url._replace(scheme = '', netloc = '')
            payload['next'] = urllib.parse.urlunparse(url)
        return make_response(jsonify(payload), 200)
    return render_template('not_found.html', show_brand = True, message = "Bad query!"), 404


@bp.route('/variants/gene/<string:variants_type>/<string:gene_name>', methods = ['POST', 'GET'])
@require_authorization
def gene_variants(variants_type, gene_name):
    sort = []
    args = []
    size = None
    url = None

    filter_type = {
       '=': 'eq',
       '!=': 'ne',
       '<': 'lt',
       '>': 'gt',
       '<=': 'lte',
       '>=': 'gte'
    }

    if request.method == 'POST':
        params = request.get_json()
        if params:
            print('params from browser = ', params)
            if 'next' in params and params['next'] is not None:
                url = params['next']
            if 'size' in params:
                size = int(params['size'])
            if 'introns' in params:
                args.append(f'introns={params["introns"]}')
            for f in params.get('filters', []):
                if isinstance(f, list):
                    if len(f) > 0 and len(set( x["field"] for x in f )) == 1:
                        args.append('{}={}'.format(f[0]['field'], ','.join(f'{filter_type.get(x["type"], "eq")}:{x["value"]}' for x in f)))
                else:
                    args.append(f'{f["field"]}={filter_type.get(f["type"], "eq")}:{f["value"]}')
            for s in params.get('sorters', []):
                sort.append(f'{s["field"]}:{s["dir"]}')

    if url is not None:
        url = f"{current_app.config['BRAVO_API_URI']}{url}"
    else:
        url = f"{current_app.config['BRAVO_API_URI']}/gene/{variants_type}?name={gene_name}"
        if size:
            url += f'&limit={size}'
        if args:
            url += f"&{'&'.join(args)}"
        if sort:
            url += f"&sort={','.join(sort)}"

    print('url to API = ', url)

    api_response = requests.get(url)
    if api_response.status_code == 200:
        payload = api_response.json()
        if not payload['error'] and payload['next'] is not None:
            # remove host url because we don't want to expose what is our api endpoint
            url = urllib.parse.urlparse(payload['next'])
            url = url._replace(scheme = '', netloc = '')
            payload['next'] = urllib.parse.urlunparse(url)
        return make_response(jsonify(payload), 200)
    return render_template('not_found.html', show_brand = True, message = "Bad query!"), 404
