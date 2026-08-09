#!/usr/bin/env python3
"""
====================================================================
 RAVL VOLLEYBALL AUCTION — Flask + SQLite server
====================================================================
 A lightweight, self-contained auction server for a volleyball
 tournament player auction. All data lives in a single SQLite file
 (auction.db) so it survives restarts, and because there is a real
 backend, multiple devices (admin laptop + captains on phones) stay
 in sync automatically.

 Why Flask instead of a pure static file?
   - Real multi-device / multi-tab sync (no localStorage tricks).
   - A real database so the tournament survives server restarts.
   - Captain passcodes are checked on the server, not the client.

 The design (scoreboard, tabs, colour scheme) is carried over from
 the original single-file ravl-auction.html so the UI stays familiar.

 HOW TO RUN LOCALLY
   pip install flask
   python app.py
   open http://127.0.0.1:5000

 HOW TO DEPLOY (GCP Linux / any Linux box)
   pip install flask gunicorn
   gunicorn --bind 0.0.0.0:8080 app:app
   then put it behind a reverse proxy (nginx) or a Cloud Run /
   systemd unit. See README.md for the full walkthrough.

 CSV FORMAT (imported on the Players tab)
   player_name,team,position,base_price,notes
   John Smith,Team A,Setter,100,Left-handed
   Alice Johnson,Team B,Outside Hitter,150,
   Only player_name is required.
====================================================================
"""

import io
import os
import csv
import json
import time
import random
import secrets
import sqlite3
from functools import wraps

from flask import (
    Flask, request, session, jsonify, render_template, Response, g,
    send_from_directory,
)

try:
    from PIL import Image
except ImportError:
    Image = None

# ------------------------------------------------------------------
# Configuration & paths
# ------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'auction.db')
SECRET_FILE = os.path.join(BASE_DIR, '.secret_key')
PHOTOS_DIR = os.path.join(BASE_DIR, 'photos')

app = Flask(__name__)

app.config['JSON_AS_ASCII'] = False            # keep ₹, €, $ etc. unescaped
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB upload cap (photos)
app.config['TEMPLATES_AUTO_RELOAD'] = True     # template edits show up without a restart
os.makedirs(PHOTOS_DIR, exist_ok=True)         # folder for uploaded photos


def _load_secret_key():
    """Persist a random session key so logins survive a restart."""
    if os.environ.get('SECRET_KEY'):
        return os.environ['SECRET_KEY']
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, 'r', encoding='utf-8') as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_FILE, 'w', encoding='utf-8') as fh:
        fh.write(key)
    return key


app.secret_key = _load_secret_key()


# ------------------------------------------------------------------
# Database helpers
# ------------------------------------------------------------------
def get_db():
    """Return a per-request SQLite connection (committed on success)."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Create the schema (idempotent) and seed default settings."""
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS captains (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            passcode  TEXT NOT NULL DEFAULT '',
            budget    INTEGER NOT NULL DEFAULT 0,
            remaining INTEGER NOT NULL DEFAULT 0,
            photo     TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS players (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            team          TEXT NOT NULL DEFAULT '',
            position      TEXT NOT NULL DEFAULT '',
            base_price    INTEGER NOT NULL DEFAULT 0,
            notes         TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'available',
            sold_to       INTEGER,
            sold_price    INTEGER,
            unsold_count  INTEGER NOT NULL DEFAULT 0,
            photo         TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS wishlists (
            captain_id INTEGER NOT NULL,
            player_id  INTEGER NOT NULL,
            PRIMARY KEY (captain_id, player_id)
        );
        CREATE TABLE IF NOT EXISTS bid_log (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts   TEXT NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auction (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            current_player_id INTEGER,
            current_bid       INTEGER NOT NULL DEFAULT 0,
            current_bidder_id INTEGER,
            timer_end         INTEGER,
            running           INTEGER NOT NULL DEFAULT 0,
            last_result       TEXT
        );
    ''')
    # Migrations: add columns to any tables created before those features
    # existed (CREATE TABLE IF NOT EXISTS won't add columns).
    p_cols = [row['name'] for row in db.execute('PRAGMA table_info(players)')]
    if 'unsold_count' not in p_cols:
        db.execute(
            "ALTER TABLE players ADD COLUMN unsold_count INTEGER NOT NULL DEFAULT 0"
        )
    if 'photo' not in p_cols:
        db.execute("ALTER TABLE players ADD COLUMN photo TEXT NOT NULL DEFAULT ''")
    c_cols = [row['name'] for row in db.execute('PRAGMA table_info(captains)')]
    if 'photo' not in c_cols:
        db.execute("ALTER TABLE captains ADD COLUMN photo TEXT NOT NULL DEFAULT ''")
    # Seed a single auction row if it doesn't exist yet.
    db.execute('INSERT OR IGNORE INTO auction (id) VALUES (1)')
    # Seed default settings.
    defaults = {
        'tournament_name': 'RAVL Season 4',
        'currency': '₹',
        'increment': 50,
        'timer_seconds': 30,
        'default_budget': 10000,
        'close_mode': 'timer',       # 'timer' or 'admin'
        'timer_mode': 'extend',      # 'extend' or 'fixed'
        'viewer_mode': 'live',       # what the public /view screen shows
        'admin_passcode': '',
    }
    for key, value in defaults.items():
        db.execute(
            'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
            (key, str(value)),
        )
    db.commit()


with app.app_context():
    init_db()


def settings_dict():
    """Return all settings as a Python dict with correct types."""
    rows = get_db().execute('SELECT key, value FROM settings').fetchall()
    s = {r['key']: r['value'] for r in rows}
    return {
        'tournament_name': s.get('tournament_name', 'RAVL Season 4'),
        'currency': s.get('currency', '₹'),
        'increment': int(s.get('increment', 50)),
        'timer_seconds': int(s.get('timer_seconds', 30)),
        'default_budget': int(s.get('default_budget', 10000)),
        'close_mode': s.get('close_mode', 'timer'),
        'timer_mode': s.get('timer_mode', 'extend'),
        'viewer_mode': s.get('viewer_mode', 'live'),
        'admin_passcode': s.get('admin_passcode', ''),
    }


def update_setting(db, key, value):
    db.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, str(value)),
    )


def log_bid(db, text):
    db.execute(
        'INSERT INTO bid_log (ts, text) VALUES (?, ?)',
        (time.strftime('%I:%M:%S %p'), text),
    )
    # Keep the feed bounded.
    db.execute(
        'DELETE FROM bid_log WHERE id NOT IN '
        '(SELECT id FROM bid_log ORDER BY id DESC LIMIT 200)'
    )


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------
def logged_in():
    return session.get('role') in ('admin', 'captain')


def is_admin():
    return session.get('role') == 'admin'


def is_captain():
    return session.get('role') == 'captain'


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return wrapped


def captain_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not is_captain():
            return jsonify({'error': 'Captain access required'}), 403
        return f(*args, **kwargs)
    return wrapped


# ------------------------------------------------------------------
# Auction business logic
# ------------------------------------------------------------------
def next_bid_amount(db, auction):
    """Compute the next valid bid for the current player."""
    settings = settings_dict()
    current_bid = auction['current_bid']
    if auction['current_bidder_id'] is None:
        player = db.execute(
            'SELECT * FROM players WHERE id = ?',
            (auction['current_player_id'],),
        ).fetchone()
        base = player['base_price'] if player else 0
        return base or settings['increment']
    return current_bid + settings['increment']


def finalize_sale(db, sold):
    """Close the current auction round: assign player or mark unsold."""
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    player_id = auction['current_player_id']
    if player_id is None:
        return
    player = db.execute(
        'SELECT * FROM players WHERE id = ?', (player_id,)
    ).fetchone()
    settings = settings_dict()

    if sold and auction['current_bidder_id'] is not None:
        captain = db.execute(
            'SELECT * FROM captains WHERE id = ?',
            (auction['current_bidder_id'],),
        ).fetchone()
        db.execute(
            'UPDATE players SET status = ?, sold_to = ?, sold_price = ? '
            'WHERE id = ?',
            ('sold', captain['id'], auction['current_bid'], player_id),
        )
        db.execute(
            'UPDATE captains SET remaining = remaining - ? WHERE id = ?',
            (auction['current_bid'], captain['id']),
        )
        log_bid(db, 'SOLD: {} -> {} for {}{}'.format(
            player['name'], captain['name'], settings['currency'],
            auction['current_bid']))
        last_result = 'sold'
    else:
        db.execute(
            "UPDATE players SET status = 'unsold', "
            "unsold_count = unsold_count + 1 WHERE id = ?",
            (player_id,),
        )
        log_bid(db, 'UNSOLD: {} (no bids) — unsold {}x'.format(
            player['name'], player['unsold_count'] + 1))
        last_result = 'unsold'

    db.execute(
        'UPDATE auction SET current_player_id = NULL, current_bid = 0, '
        'current_bidder_id = NULL, timer_end = NULL, running = 0, '
        'last_result = ? WHERE id = 1',
        (last_result,),
    )
    db.commit()


def check_auction_expiry():
    """Lazy finalize: called on every state read. If the countdown has
    hit zero and close mode is 'timer', the last bidder wins. In 'admin'
    mode the admin closes manually, so we only refresh the display."""
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    settings = settings_dict()
    if not auction or not auction['running'] or not auction['timer_end']:
        return
    now = int(time.time() * 1000)
    if now >= auction['timer_end'] and settings['close_mode'] == 'timer':
        finalize_sale(db, sold=bool(auction['current_bidder_id']))


# ------------------------------------------------------------------
# State payload sent to the client
# ------------------------------------------------------------------
def build_state():
    db = get_db()
    settings = settings_dict()
    captains = [dict(r) for r in db.execute('SELECT * FROM captains ORDER BY id')]
    players = [dict(r) for r in db.execute('SELECT * FROM players ORDER BY id')]
    auction = dict(db.execute('SELECT * FROM auction WHERE id = 1').fetchone())
    bid_log = [dict(r) for r in db.execute(
        'SELECT ts, text FROM bid_log ORDER BY id DESC LIMIT 200')]

    # Never ship passcodes to the browser; just say whether a passcode exists.
    for c in captains:
        c['has_passcode'] = bool(c.pop('passcode', ''))

    wishlists = {}
    for row in db.execute('SELECT captain_id, player_id FROM wishlists'):
        wishlists.setdefault(row['captain_id'], []).append(row['player_id'])

    return {
        'settings': {
            'tournament_name': settings['tournament_name'],
            'currency': settings['currency'],
            'increment': settings['increment'],
            'timer_seconds': settings['timer_seconds'],
            'default_budget': settings['default_budget'],
            'close_mode': settings['close_mode'],
            'timer_mode': settings['timer_mode'],
            'viewer_mode': settings['viewer_mode'],
            'admin_passcode_set': bool(settings['admin_passcode']),
        },
        'captains': captains,
        'players': players,
        'auction': auction,
        'bid_log': bid_log,
        'wishlists': wishlists,
        'me': {
            'role': session.get('role'),
            'captain_id': session.get('captain_id'),
        },
        'server_time': int(time.time() * 1000),
    }


def ok(payload):
    return jsonify(payload)


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/view')
def view():
    """Public no-login viewer screen: live scoreboard + team rosters."""
    return render_template('viewer.html')


@app.route('/health')
def health():
    return ok({'ok': True})


# ------------------------------------------------------------------
# Auth API
# ------------------------------------------------------------------
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    role = data.get('role')
    passcode = str(data.get('passcode', '') or '')
    db = get_db()

    if role == 'admin':
        settings = settings_dict()
        if settings['admin_passcode'] and passcode != settings['admin_passcode']:
            return jsonify({'error': 'Incorrect admin passcode'}), 401
        session.clear()
        session['role'] = 'admin'
        return ok({'role': 'admin'})

    if role == 'captain':
        captain_id = data.get('captain_id')
        try:
            captain_id = int(captain_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Select a captain'}), 400
        captain = db.execute(
            'SELECT * FROM captains WHERE id = ?', (captain_id,)
        ).fetchone()
        if not captain:
            return jsonify({'error': 'Unknown captain'}), 400
        if not captain['passcode']:
            return jsonify({'error': 'No passcode set for this captain — ask the admin to set one'}), 401
        if passcode != captain['passcode']:
            return jsonify({'error': 'Incorrect passcode'}), 401
        session.clear()
        session['role'] = 'captain'
        session['captain_id'] = captain_id
        return ok({'role': 'captain', 'captain_id': captain_id})

    return jsonify({'error': 'Unknown role'}), 400


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return ok({'ok': True})


# ------------------------------------------------------------------
# Settings (admin)
# ------------------------------------------------------------------
@app.route('/api/settings', methods=['POST'])
@admin_required
def save_settings():
    data = request.get_json(silent=True) or {}
    db = get_db()

    def _int(v, default):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return default

    if 'tournament_name' in data:
        update_setting(db, 'tournament_name',
                       str(data.get('tournament_name') or 'RAVL Season 4').strip())
    if 'currency' in data:
        update_setting(db, 'currency', str(data.get('currency') or '₹').strip()[:3])
    if 'increment' in data:
        update_setting(db, 'increment', max(1, _int(data.get('increment'), 50)))
    if 'timer_seconds' in data:
        update_setting(db, 'timer_seconds', max(5, _int(data.get('timer_seconds'), 30)))
    if 'default_budget' in data:
        update_setting(db, 'default_budget', _int(data.get('default_budget'), 10000))
    if 'close_mode' in data and data.get('close_mode') in ('timer', 'admin'):
        update_setting(db, 'close_mode', data['close_mode'])
    if 'timer_mode' in data and data.get('timer_mode') in ('extend', 'fixed'):
        update_setting(db, 'timer_mode', data['timer_mode'])
    if 'viewer_mode' in data and data.get('viewer_mode') in ('live', 'final'):
        update_setting(db, 'viewer_mode', data['viewer_mode'])
    if 'admin_passcode' in data:
        update_setting(db, 'admin_passcode', str(data.get('admin_passcode') or '').strip())

    db.commit()
    return ok({'ok': True})


# ------------------------------------------------------------------
# Captains (admin)
# ------------------------------------------------------------------
@app.route('/api/captains', methods=['POST'])
@admin_required
def add_captain():
    data = request.get_json(silent=True) or {}
    db = get_db()
    name = str(data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    exists = db.execute(
        'SELECT 1 FROM captains WHERE lower(name) = lower(?)', (name,)
    ).fetchone()
    if exists:
        return jsonify({'error': 'A captain with that name already exists'}), 400

    passcode = str(data.get('passcode') or '').strip()
    if not passcode:
        return jsonify({'error': 'A passcode is required so the captain can log in'}), 400
    budget = data.get('budget')
    settings = settings_dict()
    try:
        budget = max(0, int(budget))
    except (TypeError, ValueError):
        budget = settings['default_budget']

    cur = db.execute(
        'INSERT INTO captains (name, passcode, budget, remaining) '
        'VALUES (?, ?, ?, ?)',
        (name, passcode, budget, budget),
    )
    db.commit()
    return ok({'id': cur.lastrowid})


@app.route('/api/captains/<int:captain_id>', methods=['DELETE'])
@admin_required
def delete_captain(captain_id):
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if auction['current_bidder_id'] == captain_id:
        return jsonify({'error': 'Cannot remove the current highest bidder'}), 400
    db.execute('DELETE FROM captains WHERE id = ?', (captain_id,))
    db.execute('DELETE FROM wishlists WHERE captain_id = ?', (captain_id,))
    db.commit()
    return ok({'ok': True})


@app.route('/api/captains/<int:captain_id>', methods=['PUT'])
@admin_required
def update_captain(captain_id):
    """Edit a captain's name, passcode and/or budget.

    Changing the budget preserves how much the captain has already spent:
    remaining = new_budget - spent (clamped to 0)."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    captain = db.execute(
        'SELECT * FROM captains WHERE id = ?', (captain_id,)
    ).fetchone()
    if not captain:
        return jsonify({'error': 'Captain not found'}), 404

    name = captain['name']
    passcode = captain['passcode']
    budget = captain['budget']
    remaining = captain['remaining']

    if 'name' in data:
        new_name = str(data.get('name') or '').strip()
        if not new_name:
            return jsonify({'error': 'Name is required'}), 400
        dup = db.execute(
            'SELECT 1 FROM captains WHERE lower(name) = lower(?) AND id != ?',
            (new_name, captain_id),
        ).fetchone()
        if dup:
            return jsonify({'error': 'A captain with that name already exists'}), 400
        name = new_name

    if 'passcode' in data:
        passcode = str(data.get('passcode') or '').strip()
        if not passcode:
            return jsonify({'error': 'A passcode is required so the captain can log in'}), 400

    if 'budget' in data:
        try:
            new_budget = max(0, int(data.get('budget')))
        except (TypeError, ValueError):
            new_budget = budget
        if new_budget != budget:
            remaining = max(0, remaining + (new_budget - budget))
            budget = new_budget

    db.execute(
        'UPDATE captains SET name = ?, passcode = ?, budget = ?, remaining = ? '
        'WHERE id = ?',
        (name, passcode, budget, remaining, captain_id),
    )
    db.commit()
    return ok({'ok': True})


# ------------------------------------------------------------------
# Players (admin)
# ------------------------------------------------------------------
@app.route('/api/players', methods=['POST'])
@admin_required
def add_player():
    data = request.get_json(silent=True) or {}
    db = get_db()
    name = str(data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Player name is required'}), 400

    def _price(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    cur = db.execute(
        'INSERT INTO players (name, team, position, base_price, notes, status) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (
            name,
            str(data.get('team') or '').strip(),
            str(data.get('position') or '').strip(),
            _price(data.get('base_price')),
            str(data.get('notes') or '').strip(),
            'available',
        ),
    )
    db.commit()
    return ok({'id': cur.lastrowid})


@app.route('/api/players/<int:player_id>', methods=['DELETE'])
@admin_required
def delete_player(player_id):
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if auction['current_player_id'] == player_id:
        return jsonify({'error': 'Cannot remove a player currently up for bidding'}), 400
    db.execute('DELETE FROM players WHERE id = ?', (player_id,))
    db.execute('DELETE FROM wishlists WHERE player_id = ?', (player_id,))
    db.commit()
    return ok({'ok': True})


@app.route('/api/import', methods=['POST'])
@admin_required
def import_csv():
    """Accept a CSV upload with columns player_name,team,position,base_price,notes."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > 2 * 1024 * 1024:
        return jsonify({'error': 'CSV must be under 2 MB'}), 400
    try:
        text = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        return jsonify({'error': 'CSV must be UTF-8 encoded'}), 400

    reader = csv.reader(io.StringIO(text, newline=''))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        return jsonify({'error': 'CSV appears to be empty'}), 400

    header = [h.strip().lower() for h in rows[0]]
    idx_name = header.index('player_name') if 'player_name' in header else (
        header.index('name') if 'name' in header else -1)
    if idx_name < 0:
        return jsonify({'error': 'CSV must include a player_name column'}), 400

    def _get(row, header_name):
        if header_name in header:
            i = header.index(header_name)
            return row[i].strip() if i < len(row) else ''
        return ''

    db = get_db()
    added = 0
    for row in rows[1:]:
        if len(row) <= idx_name:
            continue
        name = row[idx_name].strip()
        if not name:
            continue
        try:
            base = max(0, int(float(_get(row, 'base_price') or 0)))
        except ValueError:
            base = 0
        db.execute(
            'INSERT INTO players (name, team, position, base_price, notes, status) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (
                name,
                _get(row, 'team'),
                _get(row, 'position'),
                base,
                _get(row, 'notes'),
                'available',
            ),
        )
        added += 1
    db.commit()
    return ok({'added': added})


# ------------------------------------------------------------------
# Photos (admin)
# ------------------------------------------------------------------
def _save_photo(file, entity, entity_id, old_photo):
    """Resize an uploaded image to a small JPEG thumbnail on disk.

    Returns (filename, None) on success or (None, error) on failure. The
    filename embeds a timestamp so browsers never serve a stale cached image."""
    if Image is None:
        return None, 'Pillow is not installed — install it (pip install Pillow) to upload photos'
    try:
        img = Image.open(file.stream)
        img.thumbnail((300, 300))
        img = img.convert('RGB')
    except Exception:
        return None, 'Could not read that image — upload a valid JPG/PNG'
    prefix = 'player' if entity == 'player' else 'captain'
    # Millisecond precision avoids two uploads in the same second producing
    # the same filename (which would otherwise delete the file just written).
    filename = '{}_{}_{}.jpg'.format(prefix, entity_id, int(time.time() * 1000))
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    try:
        img.save(os.path.join(PHOTOS_DIR, filename), 'JPEG', quality=82)
    except Exception:
        return None, 'Could not save the image on the server'
    # Remove the previous photo so old thumbnails don't pile up on disk.
    # Never delete the file we just wrote (same-second re-upload).
    if old_photo and old_photo != filename:
        old_path = os.path.join(PHOTOS_DIR, os.path.basename(old_photo))
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
    return filename, None


@app.route('/api/upload_photo', methods=['POST'])
@admin_required
def upload_photo():
    """Attach a photo to a player or captain. Multipart form with fields
    entity ('player'|'captain'), entity_id and file."""
    data = request.form
    entity = data.get('entity')
    try:
        entity_id = int(data.get('entity_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Missing entity id'}), 400
    if entity not in ('player', 'captain'):
        return jsonify({'error': 'entity must be player or captain'}), 400
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'error': 'No file uploaded'}), 400

    db = get_db()
    if entity == 'player':
        row = db.execute(
            'SELECT * FROM players WHERE id = ?', (entity_id,)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Player not found'}), 404
    else:
        row = db.execute(
            'SELECT * FROM captains WHERE id = ?', (entity_id,)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Captain not found'}), 404

    filename, error = _save_photo(request.files['file'], entity, entity_id, row['photo'])
    if error:
        return jsonify({'error': error}), 400
    if entity == 'player':
        db.execute('UPDATE players SET photo = ? WHERE id = ?', (filename, entity_id))
    else:
        db.execute('UPDATE captains SET photo = ? WHERE id = ?', (filename, entity_id))
    db.commit()
    return ok({'photo': filename})


@app.route('/uploads/<path:filename>')
def uploads(filename):
    """Serve an uploaded photo. Guarded against path traversal."""
    if filename != os.path.basename(filename):
        return jsonify({'error': 'Bad file name'}), 400
    return send_from_directory(PHOTOS_DIR, filename)


# ------------------------------------------------------------------
# Auction API
# ------------------------------------------------------------------
@app.route('/api/nominate', methods=['POST'])
@admin_required
def nominate():
    data = request.get_json(silent=True) or {}
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if auction['current_player_id']:
        return jsonify({'error': 'A player is already up for bidding'}), 400

    player = None
    was_unsold = False
    if data.get('random'):
        rows = db.execute(
            "SELECT * FROM players WHERE status = 'available'"
        ).fetchall()
        if not rows:
            return jsonify({'error': 'No available players left'}), 400
        player = random.choice(rows)
    else:
        player_id = data.get('player_id')
        try:
            player_id = int(player_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Pick a player to nominate'}), 400
        player = db.execute(
            'SELECT * FROM players WHERE id = ?', (player_id,)
        ).fetchone()
        if not player:
            return jsonify({'error': 'Player not found'}), 400
        if player['status'] == 'sold':
            return jsonify({'error': 'Player is not available'}), 400
        if player['status'] == 'unsold':
            # Rebidding an unsold player: bring them back into the pool so the
            # normal bidding flow applies. The unsold_count history is kept.
            was_unsold = True
            db.execute(
                "UPDATE players SET status = 'available' WHERE id = ?",
                (player['id'],),
            )

    settings = settings_dict()
    timer_end = int(time.time() * 1000) + settings['timer_seconds'] * 1000
    db.execute(
        'UPDATE auction SET current_player_id = ?, current_bid = 0, '
        'current_bidder_id = NULL, timer_end = ?, running = 1, '
        'last_result = NULL WHERE id = 1',
        (player['id'], timer_end),
    )
    if was_unsold:
        log_bid(db, '{} re-nominated for rebid (unsold {}x) — starting price {}{}'.format(
            player['name'], player['unsold_count'], settings['currency'],
            player['base_price']))
    else:
        log_bid(db, '{} nominated — starting price {}{}'.format(
            player['name'], settings['currency'], player['base_price']))
    db.commit()
    return ok({'ok': True})


@app.route('/api/bid', methods=['POST'])
@captain_required
def bid():
    """Place a bid. The captain chooses the amount themselves (in rupees,
    integers only). The server enforces the minimum increment and the
    captain's remaining budget."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if not auction['running'] or not auction['current_player_id']:
        return jsonify({'error': 'No player is currently up for bidding'}), 400

    captain = db.execute(
        'SELECT * FROM captains WHERE id = ?', (session['captain_id'],)
    ).fetchone()
    if not captain:
        return jsonify({'error': 'Captain not found'}), 400
    if auction['current_bidder_id'] == captain['id']:
        return jsonify({'error': "You already hold the highest bid"}), 400

    try:
        amount = int(round(float(data.get('amount'))))
    except (TypeError, ValueError):
        return jsonify({'error': 'Enter a valid bid amount'}), 400
    if amount < 0:
        return jsonify({'error': 'Enter a valid bid amount'}), 400

    min_amount = next_bid_amount(db, auction)
    if amount < min_amount:
        return jsonify({'error': 'Bid must be at least {}'.format(min_amount)}), 400
    if amount > captain['remaining']:
        return jsonify({'error': 'Not enough budget remaining for that bid'}), 400

    settings = settings_dict()
    now = int(time.time() * 1000)
    # In timer-close mode a bid that lands after the countdown expired is
    # rejected — that round has already been finalised. In admin-close mode
    # the timer is only a reminder, so bids stay open until the admin closes.
    if settings['close_mode'] == 'timer' and auction['timer_end'] and now >= auction['timer_end']:
        return jsonify({'error': 'This round has already closed'}), 400
    # 'extend' (default): every bid resets the countdown to the full length.
    # 'fixed': the countdown is a hard total window and never resets.
    if settings['timer_mode'] == 'extend':
        timer_end = now + settings['timer_seconds'] * 1000
    else:
        timer_end = auction['timer_end'] or (now + settings['timer_seconds'] * 1000)
    db.execute(
        'UPDATE auction SET current_bid = ?, current_bidder_id = ?, '
        'timer_end = ? WHERE id = 1',
        (amount, captain['id'], timer_end),
    )
    player = db.execute(
        'SELECT * FROM players WHERE id = ?', (auction['current_player_id'],)
    ).fetchone()
    log_bid(db, '{} bids {}{} for {}'.format(
        captain['name'], settings['currency'], amount, player['name']))
    db.commit()
    return ok({'ok': True})


@app.route('/api/sell', methods=['POST'])
@admin_required
def sell():
    """Admin manually closes the round. {'sold': true|false}."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if not auction['running'] or not auction['current_player_id']:
        return jsonify({'error': 'No player is currently up for bidding'}), 400
    if data.get('sold') and not auction['current_bidder_id']:
        return jsonify({'error': 'No bids placed yet — mark unsold instead'}), 400
    finalize_sale(db, sold=bool(data.get('sold')))
    return ok({'ok': True})


@app.route('/api/cancel', methods=['POST'])
@admin_required
def cancel_auction():
    """Abort the current round and put the player back on the list."""
    db = get_db()
    db.execute(
        'UPDATE auction SET current_player_id = NULL, current_bid = 0, '
        'current_bidder_id = NULL, timer_end = NULL, running = 0, '
        'last_result = NULL WHERE id = 1'
    )
    log_bid(db, 'Auction round cancelled by admin')
    db.commit()
    return ok({'ok': True})


# ------------------------------------------------------------------
# Wishlist (captain)
# ------------------------------------------------------------------
@app.route('/api/wishlist', methods=['POST'])
@captain_required
def toggle_wishlist():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        player_id = int(data.get('player_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Bad player id'}), 400
    exists = db.execute(
        'SELECT 1 FROM wishlists WHERE captain_id = ? AND player_id = ?',
        (session['captain_id'], player_id),
    ).fetchone()
    if exists:
        db.execute(
            'DELETE FROM wishlists WHERE captain_id = ? AND player_id = ?',
            (session['captain_id'], player_id),
        )
        active = False
    else:
        db.execute(
            'INSERT OR IGNORE INTO wishlists (captain_id, player_id) VALUES (?, ?)',
            (session['captain_id'], player_id),
        )
        active = True
    db.commit()
    return ok({'active': active})


# ------------------------------------------------------------------
# State, reset & export
# ------------------------------------------------------------------
@app.route('/api/state')
def state():
    """The single source of truth the client polls. Also lazily finalises
    any timer that expired while nobody was watching."""
    check_auction_expiry()
    return ok(build_state())


@app.route('/api/reset', methods=['POST'])
@admin_required
def reset():
    db = get_db()
    # Remove uploaded photos along with the records.
    for tbl in ('players', 'captains'):
        for row in db.execute('SELECT photo FROM {}'.format(tbl)).fetchall():
            if row['photo']:
                path = os.path.join(PHOTOS_DIR, os.path.basename(row['photo']))
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
    db.execute('DELETE FROM players')
    db.execute('DELETE FROM captains')
    db.execute('DELETE FROM bid_log')
    db.execute('DELETE FROM wishlists')
    db.execute(
        'UPDATE auction SET current_player_id = NULL, current_bid = 0, '
        'current_bidder_id = NULL, timer_end = NULL, running = 0, '
        'last_result = NULL WHERE id = 1'
    )
    db.commit()
    return ok({'ok': True})


@app.route('/api/distribute_remaining', methods=['POST'])
@admin_required
def distribute_remaining():
    """Once every captain's budget is exhausted, randomly fill each captain up
    to the exact same squad size (total_players // captains, sold at 0). Any
    players left over (total_players % captains) stay undistributed in the pool
    instead of pushing one team above the target."""
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if auction['running']:
        return jsonify({'error': 'Close the current round first'}), 400

    settings = settings_dict()
    captains = db.execute(
        'SELECT * FROM captains ORDER BY id').fetchall()
    if not captains:
        return jsonify({'error': 'No captains in the tournament'}), 400
    if not all(c['remaining'] < settings['increment'] for c in captains):
        return jsonify({'error': 'All captains must have exhausted their budgets first'}), 400

    players = db.execute(
        "SELECT * FROM players WHERE status IN ('available','unsold')"
    ).fetchall()
    if not players:
        return jsonify({'error': 'No remaining players to distribute'}), 400

    # Exact-equal distribution: every captain is filled up to the target squad
    # size (total_players // captains). Players are assigned to the captain with
    # the smallest current squad (ties random), but never beyond the target —
    # so with 41 players and 4 captains every team gets exactly 10 and the
    # remaining player stays undistributed in the pool.
    total_players = db.execute('SELECT COUNT(*) FROM players').fetchone()[0]
    target = total_players // len(captains)
    squad_sizes = {}
    for c in captains:
        squad_sizes[c['id']] = db.execute(
            'SELECT COUNT(*) FROM players WHERE sold_to = ?', (c['id'],)
        ).fetchone()[0]

    random.shuffle(players)
    distributed = 0
    for p in players:
        open_captains = [c for c in captains if squad_sizes[c['id']] < target]
        if not open_captains:
            break  # every captain is at the target — leave the rest undistributed
        min_size = min(squad_sizes[c['id']] for c in open_captains)
        smallest = [c for c in open_captains if squad_sizes[c['id']] == min_size]
        cap = random.choice(smallest)
        db.execute(
            'UPDATE players SET status = ?, sold_to = ?, sold_price = ? '
            'WHERE id = ?',
            ('sold', cap['id'], 0, p['id']),
        )
        squad_sizes[cap['id']] += 1
        distributed += 1
    left = len(players) - distributed
    log_bid(db, 'Random distribution: {} leftover player(s) balanced equally ({} per captain, ₹0); {} left undistributed'.format(
        distributed, target, left))
    db.commit()
    return ok({'distributed': distributed, 'undistributed': left, 'target': target})


@app.route('/api/assign_player', methods=['POST'])
@admin_required
def assign_player():
    """Manually assign a leftover player to a chosen captain.

    Only leftover players can be moved: players still in the pool
    ('available' / 'unsold') or players that were auto-distributed at ₹0, so
    the admin can tweak the final split. Auction purchases (sold at a price)
    are never touched. An optional price deducts from the captain's budget."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if auction['running']:
        return jsonify({'error': 'Close the current round first'}), 400

    try:
        player_id = int(data.get('player_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Pick a leftover player'}), 400
    player = db.execute(
        'SELECT * FROM players WHERE id = ?', (player_id,)
    ).fetchone()
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    if player['status'] not in ('available', 'unsold') and not (
            player['status'] == 'sold' and not player['sold_price']):
        return jsonify({'error': 'Only leftover players can be assigned'}), 400

    try:
        captain_id = int(data.get('captain_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Pick a captain'}), 400
    captain = db.execute(
        'SELECT * FROM captains WHERE id = ?', (captain_id,)
    ).fetchone()
    if not captain:
        return jsonify({'error': 'Captain not found'}), 404

    price = 0
    if data.get('price'):
        try:
            price = max(0, int(round(float(data.get('price')))))
        except (TypeError, ValueError):
            return jsonify({'error': 'Enter a valid price'}), 400
        if price > captain['remaining']:
            return jsonify({'error': 'Not enough budget remaining for that price'}), 400

    db.execute(
        'UPDATE players SET status = ?, sold_to = ?, sold_price = ? '
        'WHERE id = ?',
        ('sold', captain['id'], price, player['id']),
    )
    if price:
        db.execute(
            'UPDATE captains SET remaining = remaining - ? WHERE id = ?',
            (price, captain['id']),
        )
    log_bid(db, 'Admin assigned {} -> {} ({}{})'.format(
        player['name'], captain['name'], settings_dict()['currency'], price))
    db.commit()
    return ok({'ok': True})


@app.route('/api/unassign_player', methods=['POST'])
@admin_required
def unassign_player():
    """Return a distributed (₹0) leftover player back to the pool so the admin
    can rebalance team sizes. Auction purchases are never touched."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    auction = db.execute('SELECT * FROM auction WHERE id = 1').fetchone()
    if auction['running']:
        return jsonify({'error': 'Close the current round first'}), 400

    try:
        player_id = int(data.get('player_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Bad player id'}), 400
    player = db.execute(
        'SELECT * FROM players WHERE id = ?', (player_id,)
    ).fetchone()
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    if player['status'] != 'sold' or player['sold_price']:
        return jsonify({'error': 'Only auto-distributed (₹0) players can be returned'}), 400

    captain = db.execute(
        'SELECT * FROM captains WHERE id = ?', (player['sold_to'],)
    ).fetchone()
    db.execute(
        "UPDATE players SET status = 'available', sold_to = NULL, sold_price = NULL "
        'WHERE id = ?',
        (player['id'],),
    )
    log_bid(db, 'Admin returned {} to the pool (was with {})'.format(
        player['name'], captain['name'] if captain else 'a captain'))
    db.commit()
    return ok({'ok': True})


def _csv_text():
    """Build the results CSV in memory and return the string."""
    db = get_db()
    settings = settings_dict()
    cur = settings['currency']
    players = db.execute('SELECT * FROM players ORDER BY id').fetchall()
    captains = db.execute('SELECT * FROM captains ORDER BY id').fetchall()
    name_of = {c['id']: c['name'] for c in captains}

    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['player_name', 'team', 'position', 'base_price',
                     'status', 'sold_price', 'captain', 'unsold_count'])
    for p in players:
        writer.writerow([
            p['name'], p['team'], p['position'], p['base_price'],
            p['status'], p['sold_price'] or '',
            name_of.get(p['sold_to'], '') if p['sold_to'] else '',
            p['unsold_count'] or 0,
        ])
    writer.writerow([])
    writer.writerow(['captain', 'budget', 'spent', 'remaining', 'squad_size'])
    for c in captains:
        squad = sum(1 for p in players if p['sold_to'] == c['id'])
        writer.writerow([
            c['name'], c['budget'], c['budget'] - c['remaining'],
            c['remaining'], squad,
        ])
    return out.getvalue()


@app.route('/api/export.csv')
@admin_required
def export_csv():
    data = _csv_text().encode('utf-8-sig')   # BOM so Excel reads it correctly
    return Response(
        data,
        mimetype='text/csv',
        headers={'Content-Disposition':
                 'attachment; filename=auction_results.csv'},
    )


@app.route('/api/export.json')
@admin_required
def export_json():
    payload = build_state()
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition':
                 'attachment; filename=auction_state.json'},
    )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
