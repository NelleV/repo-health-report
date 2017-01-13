from concurrent.futures import ProcessPoolExecutor
from collections import OrderedDict
import datetime
import os
import json
import textwrap
import time


import jinja2
import tornado.autoreload
import tornado.ioloop
import tornado.web
from tornado.escape import json_encode

import nbformat
import nbformat.v4 as nbf

import git 
from github import Github
import github.GithubException
from github_oauth import BaseHandler as OAuthBase, GithubAuthHandler, GithubAuthLogout

import git_analysis


class BaseHandler(OAuthBase):
    def render_template(self, template_name, **kwargs):
        template_dirs = self.settings["template_path"]
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dirs))
        template = env.get_template(template_name)
        content = template.render(kwargs)
        return content

    def render(self, template_name, **kwargs):
        """
        This is for making some extra context variables available to
        the template.

        """
        kwargs.update({
            'settings': self.settings,
            'STATIC_URL': self.settings.get('static_url_prefix', '/static/'),
            'request': self.request,
            'xsrf_token': self.xsrf_token,
            'xsrf_form_html': self.xsrf_form_html,
            'authenticated': self.get_current_user() is not None,
            'user': self.get_current_user(),
            'handler': self
        })
        content = self.render_template(template_name, **kwargs)

        self.write(content)


class Error404(BaseHandler):
    def prepare(self):
        self.set_status(404)
        self.finish(self.render('404.html'))


def fetch_repo_data(uuid, token):

    def update_status(message=None, clear=False):
        status_file = os.path.join('ephemeral_storage', uuid + '.status.json')
        if not os.path.exists(status_file) or clear:
            existing_status = []
        else:
            with open(status_file, 'r') as fh:
                existing_status = json.load(fh)

            # Log the last status item as complete.
            existing_status[-1]['end'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

        # Allow for the option of not adding a status message so that we can call this
        # function close off the previous message once it is complete.
        if message is not None:
            existing_status.append(dict(start=datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                        status=message))

        with open(status_file, 'w') as fh:
            json.dump(existing_status, fh)

    cache = os.path.join('ephemeral_storage', uuid + '.github.json')
    dirname = os.path.dirname(cache)
    # Ensure the storage location exists.
    if not os.path.exists(dirname):
        os.makedirs(dirname)

    update_status('Initial validation of repo', clear=True)
    g = Github(token)
    repo = g.get_repo(uuid)

    if os.path.exists(cache):
        update_status('Load GitHub API data from ephemeral cache')
        with open(cache, 'r') as fh:
            report = json.load(fh)
    else:

        report = {}

        update_status('Fetching GitHub API data')
        report['repo'] = repo.raw_data

        update_status('Fetching GitHub issues data')
        issues = repo.get_issues(state='all', since=datetime.datetime.utcnow() - datetime.timedelta(days=30))
        
        limit = 5
        issues_raw = [issue.raw_data for issue, _ in zip(issues, range(limit))]
        report['issues'] = issues_raw

        update_status('Fetching GitHub stargazer data')
        stargazers = repo.get_stargazers_with_dates()
        stargazer_data = [{'starred_at': stargazer.raw_data['starred_at'], 'login': stargazer.raw_data['user']['login']}
                          for stargazer in stargazers]
        report['stargazers'] = stargazer_data

        with open(cache, 'w') as fh:
            json.dump(report, fh)

    cache = os.path.join('ephemeral_storage', uuid + '_computed.json')
    if not os.path.exists(cache):
        target = os.path.join('ephemeral_storage', uuid)
        if os.path.exists(target):
            update_status('Fetching remotes from cached clone')
            repo = git.Repo(target)
            for remote in repo.remotes:
                remote.fetch()
        else:
            update_status('Cloning repo')
            repo = git.Repo.clone_from(repo.clone_url, target)     

        update_status('Analysing commits')
        repo_data = git_analysis.commits(repo)
        with open(cache, 'w') as fh:
            json.dump(repo_data, fh)
    else:
        update_status('Load commit from ephemeral cache')
        with open(cache, 'r') as fh:
            repo_data = json.load(fh)

    # Round off the status so that the last task has an end time.
    update_status()

    repo_data['github'] = report
    return repo_data


def pretty_timedelta(datetime, from_date):
    diff = from_date - datetime
    s = diff.seconds
    if diff.days > 7 or diff.days < 0:
        return datetime.strftime('%d %b %y')
    elif diff.days == 1:
        return '1 day ago'
    elif diff.days > 1:
        return '{} days ago'.format(diff.days)
    elif s <= 1:
        return 'just now'
    elif s < 120:
        return '{} seconds ago'.format(s)
    elif s < 3600:
        return '{} minutes ago'.format(s//60)
    elif s < 7200:
        return '1 hour ago'
    else:
        return '{} hours ago'.format(s//3600)


class RepoReport(BaseHandler):
    def report_not_ready(self, uuid):
        user = self.get_current_user()
        token = user['access_token']
        self.finish(self.render('report.pending.html', token=token, repo_slug=uuid))

    @tornado.web.authenticated
    def get(self, org_user, repo_name):
        uuid = '{}/{}'.format(org_user, repo_name)
        format = self.get_argument('format', 'html')
        if format not in ['notebook', 'html']:
            self.set_status(400)
            return self.finish(self.render('error.html', error="Invalid format specified. Please choose either 'notebook' or 'html'.", repo_slug=uuid))
        datastore = self.settings['datastore']
        if uuid not in datastore:
            # Do what we do with the data handler (return 202 until we are ready)
            return self.report_not_ready(uuid)
        else:
            future = datastore[uuid]
            if not future.done():
                # Do what we do with the data handler (return 202 until we are ready)
                return self.report_not_ready(uuid)
            else:
                try:
                    payload = datastore[uuid].result()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as err:
                    import traceback
                    self.set_status(500)
                    self.finish(self.render('error.html', error=str(err), traceback=traceback.format_exc(), repo_slug=uuid))
                    return

                import plotly
                import plotly.plotly as py
                import plotly.graph_objs as go
                import plotly.offline.offline as pl_offline
                
                def html(fig):
                    config = dict(showLink=False, displaylogo=False)
                    plot_html, plotdivid, width, height = pl_offline._plot_html(
                        fig, config, validate=True,
                        default_width='100%', default_height='100%', global_requirejs=False)

                    script_split = plot_html.find('<script ')
                    plot_content = {'div': plot_html[:script_split],
                                    'script': plot_html[script_split:],
                                    'id': plotdivid}
                    return plot_content

                from analysis import PLOTLY_PLOTS

                visualisations = OrderedDict()

                for key, title, mod in PLOTLY_PLOTS:
                    prep_fn_name = '{}_prep'.format(key)
                    viz_fn_name = '{}_viz'.format(key)
                    prepare = getattr(mod, prep_fn_name)
                    viz = getattr(mod, viz_fn_name)
                    
                    data = prepare(payload)
                    fig = viz(data)

                    visualisation = html(fig)
                    del fig

                    with open(mod.__file__, 'r') as fh:
                        mod_source = fh.readlines()
                    code = ''.join(mod_source + 
                                     ["\n\n",
                                      "{} = {}(payload)\n".format(key, prep_fn_name),
                                      "iplot({}({}))\n".format(viz_fn_name, key),
                                      ])

                    visualisation['code'] = code
                    visualisation['title'] = title

                    visualisations[key] = visualisation


                if format == 'notebook':
                    nb = nbf.new_notebook()
                    nb.cells.append(nbf.new_markdown_cell(textwrap.dedent('''
                            ![Health report](https://repo-health-report.herokuapp.com/static/img/heart.png)

                            <h1>Health report for {slug}</h1>

                            <h3>About this notebook</h3>

                            This notebook was originally generated by https://repo-health-report.herokuapp.com/.
                            You can see the latest version of this report at https://repo-health-report.herokuapp.com/report/{slug}.

                            **Please note:** This notebook requires python 3 and plotly.
                            '''.format(slug=uuid))))

                    nb.cells.append(nbf.new_code_cell(
                        '\n'.join(['# The following data can be retrieved from https://repo-health-report.herokuapp.com/api/data/{}'.format(uuid),
                         'import json',
                         'payload = json.loads(r"""',
                         json.dumps(payload),
                         '""".strip())',
                         ''])))
                    nb.cells.append(nbf.new_markdown_cell("Now, let's initialise plotly, and to recreate the visualisations on https://repo-health-report.herokuapp.com."))
                    nb.cells.append(nbf.new_code_cell(['from plotly.offline import iplot, init_notebook_mode\n', 'init_notebook_mode()']))
                    for visualisation in visualisations.values():
                        nb.cells.append(nbf.new_markdown_cell(visualisation['title']))
                        nb.cells.append(nbf.new_code_cell(visualisation['code']))
                       
                    content = nbformat.writes(nb, version=4)
                    self.set_header("Content-Type", 'application/x-ipynb+json')
                    self.set_header("Content-Disposition", 'attachment; filename="health_{}.ipynb"'.format(uuid.replace('/', '_')))
                    return self.finish(content)
                    
                else:
                    self.finish(self.render('report.html', payload=payload, viz=visualisations, repo_slug=uuid))


class Status(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        user = self.get_current_user()
        # TODO: We should define an admin group...
        if user['login'] == 'pelson':
            self.finish(self.render('status.html', futures=self.settings['datastore']))


class APIDataAvailableHandler(BaseHandler):
    known_uuid = []
    known_tokens = []

    def check_xsrf_cookie(self, *args, **kwargs):
        # We don't want xsrf checking for this API - the user can come from anywhere, provided they give us a token.
        pass

    # No authentication needed - pass the github token as TOKEN.
    def post(self, uuid):
        self.set_header('Content-Type', 'application/json')
        token = self.get_argument('token', None)
        response = self.availablitiy(uuid, token)
        self.set_status(response['status'])
        self.finish(json_encode(response))

    def availablitiy(self, uuid, token):
        """
        Return a status payload to confirm whether or not the data exists ({'status': 200, ...} for yes)

        """

        if False:
            while token is not None:
                gh = Github(token)

                # Try to get the user's rate limit. This will fail if we have a bad token.
                try:
                    rate_limiting = gh.rate_limiting
                except github.GithubException as err:
                    message = str(err)
                    token = None
                    break

                if rate_limiting[0] / float(rate_limiting[0]) < 0.1:
                    message = 'Less than 10% left of your GitHub rate limit ({}/{}).'.format(*gh.rate_limiting)
                    token = None
                    break

                if not set(gh.oauth_scopes).issuperset(set(self.settings['github_scope'])):
                    message = 'Incorrect scopes for the token given token (it has "{}").'.format(', '.join(gh.oauth_scopes))
                    token = None

                break
            
            if token is None:
                response = {'status': 401, 'message': message}
                return response

            gh = Github(token)

            try:
                repo = gh.get_repo(uuid)
                repo.id
            except github.GithubException:
                return {'status': 422, 'message': "The repo '{}' could not be found.".format(uuid)}

        datastore = self.settings['datastore']
        executor = self.settings['executor']

        if uuid not in datastore:
            future = executor.submit(fetch_repo_data, uuid, token)
            future._start_time = datetime.datetime.utcnow()
            datastore[uuid] = future

            # The status code should be set to "Submitted, and processing"
            self.set_status(202)
            response = {'status': 202, 'message': 'Job submitted and is processing.', 'status_info': []}
            return response
        else:
            future = datastore[uuid]

            status_file = os.path.join('ephemeral_storage', uuid + '.status.json')
            if not os.path.exists(status_file):
                status = {}
            else:
                with open(status_file, 'r') as fh:
                    status = json.load(fh)

            if future.done():
                return {'status': 200, 'message': "ready", 'status_info': status}
            else:
                response = {'status': 202,
                            'message': ('Job is still running and started {}.'
                                        ''.format(pretty_timedelta(future._start_time, datetime.datetime.utcnow()))),
                            'status_info': status,
                            }
                return response


class APIDataHandler(APIDataAvailableHandler):
    @tornado.web.authenticated
    def get(self, uuid):
        token = self.get_current_user()['access_token']
        self.resp(uuid, token)
    
    def post(self, uuid):
        token = self.get_argument('token', None)
        self.resp(uuid, token)

    def resp(self, uuid, token):
        self.set_header('Content-Type', 'application/json')
        response = self.availablitiy(uuid, token)

        if response['status'] != 200:
            self.set_status(response['status'])
            self.finish(json_encode(response)) 
        else:
            future = datastore = self.settings['datastore'][uuid]
            # Just because we have the result, doesn't mean it wasn't an exception...
            try:                    
                self.finish(json_encode({'status': 200,
                                         'content': future.result()}))
            except Exception as err:
                import traceback
                response = {'status': 500, 'message': str(err), 'traceback': traceback.format_exc()}
                return response


class MainHandler(BaseHandler):
    def get(self):
        self.render("index.html")

    def post(self):
        slug = self.get_argument('slug', None)
        if slug is None or slug.count('/') != 1:
            self.set_status(400)
            self.finish(self.render('index.html', input_error='Please enter a valid GitHub repository.', repo_slug=slug))
        else:
            self.redirect('/report/{}'.format(slug))


def make_app(**kwargs):
    app = tornado.web.Application([
        tornado.web.URLSpec(r'/oauth', GithubAuthHandler, name='auth_github'),
        tornado.web.URLSpec(r'/', MainHandler, name='main'),
        (r'/static/(.*)', tornado.web.StaticFileHandler),
        (r'/api/request/(.*)', APIDataAvailableHandler),
        (r'/api/data/(.*)', APIDataHandler),
        tornado.web.URLSpec(r'/report/([^/]+)/([^/]+)', RepoReport),
        (r'/logout', GithubAuthLogout),
        (r'/status', Status),
        ],
        login_url='/oauth', xsrf_cookies=True,
        template_path='templates',
        static_path='static',
        **kwargs)
    return app


if __name__ == '__main__':
    # Our datastore is simply a dictionary of {Repo UUID: Future objects}
    datastore = {}

    app = make_app(github_client_id=os.environ['CLIENT_ID'],
                   github_client_secret=os.environ['CLIENT_SECRET'],
                   cookie_secret=os.environ['COOKIE_SECRET'],
                   github_scope=['repo', 'user:email'],
                   autoreload=True, debug=True,
                   default_handler_class=Error404,
                   datastore=datastore)
    app.listen(os.environ.get('PORT', 8888))

    executor = ProcessPoolExecutor()
    app.settings['executor'] = executor

    tornado.autoreload.add_reload_hook(executor.shutdown)
    tornado.ioloop.IOLoop.current().start()
