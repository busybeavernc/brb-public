<p align="center"><img src="static/img/logo.png" alt="BRB - Be Right Back" width="420"></p>

**BRB** records a short, seamless loop of you, then lets you switch between that loop and your live webcam in Zoom, Teams or any browser meeting. For Windows 10/11 and macOS 13+.

**Download: [brb.happyhacks.io](https://brb.happyhacks.io/)**. The [setup guide](GUIDE.md) is also at [brb.happyhacks.io/guide](https://brb.happyhacks.io/guide).

This repository holds BRB's public [releases](https://github.com/busybeavernc/brb-public/releases) (published by the app's build) and the website. The app's code lives in a separate repository.

## The website (brb.happyhacks.io)

A small Flask app, set up like happyhacks.io and zipzip:
- `/`: download page. Picks the right file for the visitor's computer and counts downloads.
- `/guide`: [GUIDE.md](GUIDE.md).
- **Sign in with Happy Hacks** (`/auth/happyhacks`): one account for every Happy Hacks app. Optionally require it to download (`DOWNLOADS_REQUIRE_SIGNIN=1`).
- `/admin`: the latest release with download counts, and **Update website**, which pulls this repository from GitHub and restarts the site.

### First-time setup on the CloudPanel server
1. **CloudPanel → Add Site → Python**: domain `brb.happyhacks.io`, App Port **9020**. Note the site user it creates.
2. **Get the code**, as that site user (SSH in as the site user, or `sudo su - <site user>`):
   ```
   git clone https://github.com/busybeavernc/brb-public.git ~/doppleganger
   ```
3. **Install**, as root:
   ```
   sudo SITE_USER=<site user> bash /home/<site user>/doppleganger/deploy/cloudpanel/install.sh
   ```
   This sets up the Python environment, writes `.env` (with a generated `SECRET_KEY` and an admin password, printed once), runs the tests, starts the `doppleganger` program under supervisor on 127.0.0.1:9020, and installs `sudo doppleganger-deploy`.
4. **Connect Sign in with Happy Hacks.** On the happyhacks.io server, from the happyhacks folder:
   ```
   .venv/bin/python -m happyhacks.sso add doppleganger "BRB" https://brb.happyhacks.io/auth/happyhacks/callback
   sudo supervisorctl restart happyhacks
   ```
   Paste the three `HAPPYHACKS_...` lines it prints into `/home/<site user>/doppleganger/.env`, add your email to `ADMIN_CONTACTS`, then `sudo supervisorctl restart doppleganger`.
5. **DNS and HTTPS:** an `A` record for `doppleganger` pointing at the server, then **CloudPanel → Sites → brb.happyhacks.io → SSL/TLS → New Let's Encrypt Certificate**.

### Updates
**/admin → Update website**, or over SSH: `sudo doppleganger-deploy`. It pulls `main`, reinstalls, runs the tests and restarts; the site is down for a few seconds.

### Admins
Emails or mobiles in `ADMIN_CONTACTS` become admins when they next sign in with Happy Hacks (confirmed contacts only). Or run `.venv/bin/python -m dopplesite.users admin you@example.com`. `ADMIN_PASSWORD` in `.env` is a backup way in.

### Running it locally
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
SESSION_COOKIE_SECURE=0 .venv/bin/python wsgi.py        # http://127.0.0.1:5000
.venv/bin/python -m unittest discover -s tests -t .
```
