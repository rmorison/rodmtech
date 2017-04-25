## Deploy setup instructions

    sudo apt-get install build-essential python-dev libffi-dev
    mkvirtualenv rodmtech-deploy
    # fab only good for python 2.7
    python --version 2>&1 | grep '^Python 2.7' &>/dev/null && echo v2 yay || echo not v2 nay
    vi targets targets/live.yaml
    fab target:mysite setup
    # add ssh pub key to source repo
    fab target:mysite install
    fab target:mysite reboot
    fab target:mysite build deploy
