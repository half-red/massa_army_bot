# massa_army_bot
## Description
Simple bot for https://t.me/MassArmyHeroes
Deployed as https://t.me/MassArmyBot

NB: *Only tested on Ubuntu derivatives*

## Installation
### Manual installation
#### Project dependencies
You'll need direnv installed:
```sh
sudo apt update
sudo apt install direnv
```
aswell as python 3.12
If not done apready, add the the deadsnakes ppa:
```sh
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
```
then install python3.12
```sh
sudo apt install python3.12 python3.12-{dev,venv,distutils}
```

Copy `.envrc.defaults` to `.envrc`
```sh
cp .envrc.defaults .envrc
```
Edit all `...` in the new `.envrc` file with your own values for these variables
```bash
export\
  TG_API_ID=...\
  TG_API_HASH="..."\
  TG_BOT_TOKEN="..."\
  TG_BOT_USERNAME="..."\
  TG_LOG_CHANNEL="..."
```
Variable        | Description         | Where to find/set
======================================================================
TG_API_ID       |                     | https://my.telegram.org
TG_API_HASH     |                     | https://my.telegram.org
TG_BOT_TOKEN    |                     | https://t.me/BotFather
TG_BOT_USERNAME | username without @  | https://t.me/BotFather
TG_LOG_CHANNEL  | username or chat id | id: /id with t.me/MissRose_bot

Then run
```sh
direnv allow
```
Rerun `direnv allow` anytime you modify the file

#### Python dependencies
**IMPORTANT: At this point your direnv should be enabled!**
For all installations, you'll need:
```sh
pip install -r requirements.txt
pip install -e .
```
For development, you'll also need:
```sh
pip install -r dev-requirements.txt
```

### Docker
*Work in progress*

## Running
### Manual run
```sh
./run.py src/massa_army_bot/bot
```
Debugging with hot reload on save
```sh
./run.py -w path/to/script
```
More options are available for dispatching args to python, pytest and pyright
Please read `./run.py --help` for more info

### Docker
*Work in progress*
