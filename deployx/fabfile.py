
from glob import glob
from time import sleep
from os.path import join
from contextlib import contextmanager
from itertools import izip, count
from fabex.api import *
from fabex.contrib.files import *
from query_yes_no import query_yes_no

env.all_roles = ['lb', 'app', 'db'] # convenience for @task_roles
env.shell = "/bin/bash -l -i -c"
env.use_ssh_config = True

fabex_config(config={'target_dir': 'targets',
                     'template_dir': 'templates',
                     'template_config': 'templates.yaml'})

## helpers and utils

@contextmanager
def workon(cd_to=None):
    and_cd = "&& cd {}".format(cd_to) if cd_to else ""
    with prefix("workon {project_name} {}".format(and_cd, **env)):
        yield

@contextmanager
def pip_install(force=False):
    get_reqs = lambda f: run("cat {}".format(f), quiet=True)
    if 'requirements_txt' in env:
        req_files = (env.requirements_txt if isinstance(env.requirements_txt, list)
                    else [env.requirements_txt])
        old_reqs = []
        for req_file in req_files:
            if not exists(req_file) and not env.dryrun:
                abort("pip requirements {} not found".format(req_file))
            old_reqs.append(get_reqs(req_file))
    yield
    if 'requirements_txt' in env:
        for req_file, old_req in izip(req_files, old_reqs):
            if force or old_req != get_reqs(req_file):
                run('pip install --upgrade -r {}'.format(req_file))

def pull_and_update(rev=None, clean=True):
    """Pull from upstream repo and update to rev locally: assumes in a repo"""

    if env.vcs == 'git':
        rev = rev or 'master'
        if clean:
            run("git clean -d -f")
        run("git pull origin master -f && git checkout {}".format(rev))
    else:
        rev = rev or 'default'
        clean = '-C ' if clean else ''
        run("hg pull && hg up {}{}".format(clean, rev))


## setup

@task_roles(env.all_roles, group='setup')
@runs_once_per_host
def setup_hostname():
    """Set hostname"""

    require('target', provided_by=target)

    sudo("hostname {host}".format(**env))
    sudo("echo {host} >/etc/hostname".format(**env))

@task_roles(env.all_roles, group='setup')
@runs_once_per_host
def setup_timezone():
    """Set to the target's timezone"""

    require('target', provided_by=target)

    if 'timezone' in env:
        sudo('timedatectl set-timezone {timezone}'.format(**env))
        sudo('timedatectl status')
    else:
        warn("no timezone in target")

@task_roles(env.all_roles, group='setup')
@runs_once_per_host
def setup_locale():
    """Check/set the preferred system locale"""

    require('target')

    if 'locale' in env:
        with hide('stdout'):
            need_locale = env.locale not in sudo('cat /etc/default/locale', quiet=True)
        if need_locale:
            sudo('update-locale LANG={locale} LANGUAGE={language}'.format(**env))
            sudo('dpkg-reconfigure -f noninteractive locales')
            sudo('locale')
        else:
            warn("locale seems to be alredy set to {locale}".format(**env))
    else:
        warn("no local in target")

@task_roles(env.all_roles, group='setup')
@runs_once_per_host
def setup_etc_hosts():
    """Infer hosts settings, add/update in /etc/hosts"""

    require('target', provided_by=target)

    marker = ["#### Fab start marker: edit at own peril! ####",
              "#### Fab end marker: edit at own peril! ####",]
    server_private_ips = [(attrs['ip'], name) for name, attrs in env.hostenvs.items()]
    # remove old autogen'd hosts
    sudo(r"sed -i '/{0}/,/{1}/d' /etc/hosts".format(*marker))
    hosts_lines = ([marker[0]]
                   + ["{0}\t{1}".format(*name_ip) for name_ip in server_private_ips]
                   +[marker[1]])
    append('/etc/hosts', '\n'.join(hosts_lines), use_sudo=True)

@task_roles(env.all_roles, group='setup')
@runs_once_per_host
def setup_sshkey():
    """ssh-keygen a key"""

    require('target')

    if exists("~/.ssh/id_rsa"):
        print("~/.ssh/id_rsa exists, skipping ssh key setup")
    else:
        run("ssh-keygen -t rsa -N '' -f ~/.ssh/id_rsa")
    pubkey = execute(getsshkey, host=env.host)[env.host]
    print("*** Make sure this key provides access to the vcs repo ***\n\n"+
          pubkey+"\n\n"
          "**********************************************************")

## install

@task_roles(env.all_roles, group='install')
@runs_once_per_host
def install_upgrades():
    sudo("DEBIAN_FRONTEND=noninteractive apt-get -y update")
    #sudo("DEBIAN_FRONTEND=noninteractive apt-get -y upgrade")

@task_roles(env.all_roles, group='install')
def install_packages():
    """Install system packages"""

    sudo('DEBIAN_FRONTEND=noninteractive apt-get install --yes {}'
         .format(' '.join(env.packages)))

@task_roles(env.all_roles, group='install')
def install_pips():
    if 'pips' in env:
        # make sure we have a recent pip, apt is usually behind
        sudo('pip install --upgrade pip')
        sudo('pip install {}'.format(' '.join(env.pips)))
        # fix byproduct of sudo pip install
        sudo("chown --recursive -v $SUDO_USER:root ~/.pip ~/.rnd"
             " && chmod -v 770 ~/.pip "
             " && chmod -v 660 ~/.rnd", quiet=True)

## build

@task
@runs_once_per_host
def service(script, command, maxwait=4):
    """Run `service script command`"""
    sudo('service {} {}'.format(script, command))
    if command.endswith('start') and maxwait:
        for i in range(maxwait):
            if (sudo('service {} status >/dev/null && echo up || echo down...'
                     .format(script), quiet=True)
                == 'up'):
                break
            print("waiting for {} to start".format(script))
            sleep(i+1)
        else:
            abort("service {} never started afaict".format(script))

@task_roles('lb', group='build')
def build_lb():
    """Build an (nginx) static server/load balancer/appserver proxy"""

    require('target', provided_by=target)

    # make sure dirs exist and any default site file is gone
    sudo("mkdir -v -p /etc/nginx/conf.d /etc/nginx/sites-enabled /etc/nginx/sites-available")
    sudo("rm -f /etc/nginx/sites-enabled/default")

    # add appserver ssh keys, for static files rsync
    for appserver in env.roledefs['app']:
        try:
            pubkey = execute(getsshkey, host=appserver)[appserver]
        except KeyError:
            abort("failed to get ssh key from appserver {}".format(appserver))
        run("grep '{0}' ~/.ssh/authorized_keys && echo alredy have key for {1}"
            " || echo '{0}' >>~/.ssh/authorized_keys"
            .format(pubkey, appserver), quiet=True)

    # set up SSL certificate.
    execute(build_lb_cert, host=env.host, reload=False, force=False)

@task_roles('lb')
def build_lb_cert(reload=True, force=True):
    """Add ssl to nginx. WARNING will overwrite any existing ssl setup"""

    require('target', provided_by=target)
    if not env.get('ssl_cert'):
        print("This target doesn't ask for an ssl_cert build, so skipping")
    else:
        ssl_path = "/etc/nginx/ssl"
        sudo("mkdir -v -p {0} && chmod 750 {0}".format(ssl_path))
        with cd(ssl_path):
            crt_file = 'https.crt'
            key_file = 'https.key'
            if (exists(crt_file) and exists(key_file)
                and not query_yes_no("An ssl cert is already in place, are you sure?")):
                abort("build aborted")
            altnames = ','.join(['subjectAltName=DNS.{}={}'.format(i,n)
                                 for i,n in izip(count(1), env.ssl_altnames)])
            subj = ("/C=US/ST=MA/L=Milford/O=Seracare Life Sciences/OU=SCLS Cyber R&D"
                    "/CN={domain}/emailAddress=noreply@seracare.com".format(**env)
                    + (("/" + altnames) if altnames else ""))
            sudo("openssl req -new -x509 -out https.crt -keyout https.key -sha256 -nodes "
                 "-subj '{}'".format(subj))
    upload_project_template('nginx.conf', reload=reload, force=force)

@task_roles('app', group='build')
def build_app():
    """Setup virtualenvwrapper and make a virtualenv for the project"""

    require('target', provided_by=target)

    # virtualenv setup
    upload_project_template('virtualenvrc')
    append(".bashrc", "source $HOME/.virtualenvrc")
    if not exists(env.workon_home):
        run("mkdir -v -p {workon_home}".format(**env))

    # mkvirtualenv
    with cd(env.workon_home):
        if exists(env.project_name):
            print_command("virtualenv {project_name} exists; leaving as-is".format(**env))
        else:
            python = "--python={python} ".format(**env) if 'python' in env else ""
            run("mkvirtualenv {0}{project_name}".format(python, **env))

    # register vcs repo host, clone repo, setup django project, pip install
    run("grep '{repo_host_key}' ~/.ssh/known_hosts && echo repo host key in place"
        " || echo '{repo_host_key}' >>~/.ssh/known_hosts"
        .format(**env), quiet=True)

    with prefix('workon {project_name} && cdvirtualenv'.format(**env)):
        run("mkdir -v -p log run")
        if not exists(env.repo_name):
            run("{vcs} clone {repo_url} {repo_name}".format(**env))
        run("echo {workon_home}/{project_name}/{repo_name} >.project"
            .format(**env))

    # first time pip install...pip_install cm will only install on changed requirements
    with workon(env.requirements_dir):
        with pip_install(force=True):
            pass
        sudo("mkdir -p -v {static_prefix}/{project_name}/static"
             " {media_prefix}/{project_name}/media".format(**env))

    # ubuntu 16 supervisor doesn't auto-start on install
    sudo('service supervisor start')

    if env.get('build_frontend'):
        # install node 6.3 in nvm environment
        run("curl -o- https://raw.githubusercontent.com/creationix/nvm/v0.32.0/install.sh | bash")
        run("nvm install {node_version}".format(**env))
        run("nvm use {node_version}".format(**env))
        # install global
        for pkg in env.get('npm_global', []):
            run("npm install --silent {} -g".format(pkg))
        with workon(env.frontend_dir):
            run("npm install --silent")

@task_roles('db')
def build_db():
    """1-time db server setup"""

    require('target', provided_by=target)
    env.pg_conf = "/etc/postgresql/{pg_version}/main/postgresql.conf".format(**env)
    env.hba_conf = "/etc/postgresql/{pg_version}/main/pg_hba.conf".format(**env)
    env.pg_address = env.hostenvs[env.roledefs['db'][0]].get('ip')
    if not env.pg_address:
        abort("cannot infer db server private ip address")
    sudo("chmod g+w {pg_conf} {hba_conf} && chgrp root {pg_conf} {hba_conf}".format(**env))
    append(env.pg_conf, "listen_addresses = 'localhost, {pg_address}'"
           .format(**env), use_sudo=True)
    append(env.hba_conf, "host    all             all             {pg_address}/24         md5"
           .format(**env), use_sudo=True)
    sudo('service postgresql restart')

    execute(create_db)

@task_roles('app')
@runs_once
def create_db():
    with role_settings('app'), warn_only():
        upload_project_template('dbuser.sql', force=True)
        upload_project_template('dbinit.sql', force=True)

@task_roles('app')
@runs_once
def reset_test_db():
    with role_settings('app'), warn_only():
        upload_project_template('test_dbreset.sql', force=True)

@task_roles('app', group='wipe')
@runs_once
def wipe_db(wipe='n'):

    require('target', provided_by=target)

    if wipe[0] not in 'yY':
        if not query_yes_no("This will delete the entire database, are you sure?"):
            abort("aborting wipe task")
    with role_settings('app'):
        upload_project_template('dbwipe.sql', force=True)

@task_roles('app', group='wipe')
@runs_once
def wipe_app(wipe='n'):

    require('target', provided_by=target)

    if wipe[0] not in 'yY':
        if not query_yes_no("This will delete the entire app and virtualenv, are you sure?"):
            abort("aborting wipe task")
    run("rmvirtualenv {project_name}".format(**env))


def globalsitepackages(on=True):
    keyword = 'enabled' if on else 'disabled'
    with workon():
        for n in range(2):
            if keyword in run('toggleglobalsitepackages').lower():
                break
        else:
            abort("{} failed for global site packages".format(keyword))
    print_command("global site packages are {}".format(keyword))


## deploy

@task_roles('lb', group='deploy')
def deploy_lb():
    """Deploy config changes to load balancer"""

    require('target', provided_by=target)

    with role_settings('app'):
        if env.get('htpasswd_file'):
            upload_project_template('nginx-htpasswd')
        upstream_uwsgi_servers = '\n    '.join(["server {}:{};".format(host, env.wsgi_port)
                                                for host in env.roledefs['app']])
        with settings(upstream_uwsgi_servers=upstream_uwsgi_servers):
            upload_project_template('nginx-site.conf')
        upload_project_template('nginx.conf')

@task_roles('app', group='deploy')
def deploy_app(force='n'):
    # NOTE: a bug in Fabric put() inside a cd() context manager yields the following
    # flavor of fail. Minimilize the repro and report to Fabric project...someday.
    #
    #   [ubuntu@192.168.56.15] put: <file obj> -> /home/ubuntu/pyves/c240/countdown240/timesheet/timesheet/local_settings.py
    #
    #   Fatal error: put() encountered an exception while uploading '<StringIO.StringIO instance at 0x7f8f362aa098>'
    #
    #   Underlying exception:
    #       'ascii' codec can't decode byte 0xe2 in position 99: ordinal not in range(128)
    #
    # And, hence, we move the file uploads outside of cdproject. The debug is a pita.
    with workon(env.requirements_dir), pip_install(force.startswith('y')):
        pull_and_update(env.get('revision') or env.get('branch'))

    upload_project_template('local_settings', reload=False)
    upload_project_template('uwsgi.ini', reload=False)

    # migrate before any other manage commands...
    execute(deploy_solo, host=env.host)
    with workon():
        run("./manage.py clean_pyc")
        run("./manage.py compile_pyc")
    upload_project_template('supervisord-uwsgi')
    sudo("supervisorctl restart {project_name}-uwsgi".format(**env))

@task_roles('app', group='deploy')
@runs_once
def deploy_solo():
    # build and collect django static assets
    with workon():
        run("./manage.py migrate --noinput")
        run("./manage.py collectstatic -v0 --noinput --clear")
    # build and collect f/e code and assets
    if env.get('build_frontend'):
        with workon(env.frontend_dir):
            run("nvm use {node_version}".format(**env))
            run("npm --silent update")
            run("ng build --environment=prod")
            run("rsync -e 'ssh -o StrictHostKeyChecking=no' -az {} {}"
                .format("dist", static_dest))

@task_roles('app')
def scrub_npm():
    if env.get('build_frontend'):
        with workon(env.frontend_dir):
            run("rm -rf node_modules/ && npm cache clean")

@task_roles('app') # not part of deploy group, only needs to be run once after first deploy
@runs_once
def deploy_fixtures():
    with workon():
        run("./manage.py shell <<<'"
            "import django;"
            "from django.conf import settings;"
            "from account.models import Site;"
            "site, _ = Site.objects.get_or_create(domain=\"{domain}\");"
            "site.name = site.name or \"{domain}\".split(\".\")[0];"
            "site.save();'".format(**env))


## misc utilities

@task_roles(env.all_roles)
@runs_once_per_host
def reboot():
    sudo("reboot")

@task_roles(env.all_roles)
@runs_once_per_host
def poweroff():
    sudo("poweroff")

@task_roles(env.all_roles)
@runs_once_per_host
def sudocmd(cmd):
    sudo(cmd)

@task_roles(env.all_roles)
@runs_once_per_host
def runcmd(cmd):
    run(cmd)

@task
def getsshkey():
    with host_settings(env.host):
        return run("cat ~/.ssh/id_rsa.pub")

@task_roles(env.all_roles)
@runs_once_per_host
def pullsshkey():
    try:
        pubkey = execute(getsshkey, host=env.host)[env.host]
    except KeyError:
        abort("failed to get ssh key from host {}".format(env.host))
    local("grep '{0}' ~/.ssh/authorized_keys || echo '{0}' >>~/.ssh/authorized_keys"
          .format(pubkey))

@task_roles('app')
@runs_once
def load_db(sql_file):
    zcat = "zcat" if sql_file.endswith('.gz') else "cat"
    remote_sql = join('/tmp', sql_file.rsplit('/', 1)[-1])
    put_file = True
    if exists(remote_sql):
        local_md5 = local("md5sum {}".format(sql_file), capture=True)
        remote_md5 = run("md5sum {}".format(remote_sql), quiet=True)
        put_file = local_md5.split()[0] != remote_md5.split()[0]
    if put_file:
        put(sql_file, remote_sql)
    sudo("{} {} | sudo -u postgres psql --port={db_port} {db_name}".format(zcat, remote_sql, **env))
