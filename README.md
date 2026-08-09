# RAVL Volleyball Tournament Player Auction — Flask + SQLite

A lightweight, self-contained server for running a live volleyball player
auction. The admin configures captains, budgets and auction rules, imports the
player pool from CSV, then runs the auction while captains bid live from their
own phones. Because the state lives in a real SQLite database, **every device
stays in sync automatically** — no localStorage tricks, no refreshing needed.

Highlights: rebid unsold players with a permanent unsold counter, a **public
no-login viewer screen** (`/view`) for a projector/TV, captain "My Team" panels,
and an **exact-equal team distribution** (remainder stays undistributed) with
manual admin overrides.

```
auction_volleyball/
├── app.py              # Flask server + all API endpoints + SQLite schema
├── templates/
│   ├── index.html      # Mobile-friendly admin/captain UI (all CSS/JS inline)
│   └── viewer.html     # Public no-login live viewer screen (/view)
├── requirements.txt    # Flask
├── ravl_free_pool.csv  # Ready-made sample player list (42 players)
└── ravl-auction.html   # Original single-file version (kept as reference)
```

---

## 1. Run it locally

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000** — or, on the same Wi-Fi, open
**http://<your-computer-ip>:5000** on every phone/tablet/laptop.

The first run creates `auction.db` (the whole tournament) and a `.secret_key`
(session signing) automatically.

> Windows firewall may prompt you to allow Python on private networks —
> click *Allow* so the captains' phones can connect.

## 2. Deploy on a GCP Linux VM (or any Linux box)

```bash
# one-time setup
sudo apt update
sudo apt install -y python3-pip git
pip3 install flask gunicorn

# get the code and run it
cd auction_volleyball
gunicorn --bind 0.0.0.0:8080 app:app --workers 1
```

Then open your GCP firewall for TCP port **8080** (VPC network → firewall
rules), or put nginx in front:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
}
```

For a proper long-running service, wrap gunicorn in a systemd unit:

```ini
[Unit]
Description=RAVL Auction
After=network.target

[Service]
WorkingDirectory=/home/youruser/auction_volleyball
ExecStart=/usr/bin/gunicorn --bind 127.0.0.1:8080 app:app --workers 1
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

> Use `--workers 1` (or a couple with `--threads`) — the app relies on a
> single in-process SQLite DB and simple session cookies, so multiple
> processes aren't needed for a club auction.

## 3. How to use it

1. **Admin** signs in — set an Admin passcode on the Setup tab so only you can
   reach the admin screens.
2. **Setup tab** — tournament name, currency symbol (₹ / $ / €…), minimum bid
   increment, countdown duration, close mode (`Timer expiry` = last bidder wins
   automatically, or `Admin manually closes`), default captain budget, and the
   captain list. **Increment, default budget and captain budgets are entered in
   Crores** (budget `1000` = ₹1000 Crore, increment `1` = ₹1 Crore, `0.5` =
   ₹50 Lakh). **Each captain needs a passcode** (required — no captain can
   sign in without one) plus their starting budget.
3. **Players tab** — upload a CSV or add players by hand. Remove players
   before the auction starts. Expected CSV:

   ```csv
   player_name,team,position,base_price,notes
   John Smith,Team A,Setter,100,Left-handed
   Alice Johnson,Team B,Outside Hitter,150,
   ```

   Only `player_name` is required. `ravl_free_pool.csv` is a ready-made example
   (name + notes only) you can import immediately. `base_price` is stored in
   plain rupees (e.g. `10000000` = ₹1 Crore).

   **Crore-scale setup:** Setup fields and captain budgets are entered in
   Crores (budget `1000` = ₹1000 Crore, increment `1` = ₹1 Crore) and stored
   as plain rupees. Displayed Indian-auction style — ₹1 Crore+ renders as
   `₹X.XX Crore`, ₹1 Lakh+ as `₹X.XX Lakh`, smaller amounts as plain rupees.

4. **Live Auction tab** — the admin nominates a player from the dropdown **or
   clicks 🎲 Random Player** to let the app draw one at random. Captains sign in
   on their phones (name + their passcode), see the scoreboard, and type the
   amount they want to bid into the **Bid (₹ Crore)** box in **whole crores**
   (e.g. `2` = ₹2 Crore, decimals are not allowed; a live preview shows the
   exact rupees). The bid can't go below the
   minimum increment nor above the captain's remaining budget — both enforced
   on the server. Every bid resets the countdown. On timer expiry (or admin
   **Sell**), the player goes to the winning captain and their budget is
   deducted. Captains can also star players into a personal wishlist.

   **Rebidding unsold players** — players marked *Unsold* are never lost. The
   admin can nominate them again: they appear in the nominate dropdown tagged
   `⚠ unsold ×N`, and the Results tab has a **Rebid** button per unsold player
   that jumps to the auction tab and pre-selects them. Every player tracks how
   many times they went unsold (`unsold_count`), shown in the players list,
   results tables, dropdowns, bid feed and the CSV export — history is kept
   even after they are finally sold.

   **My Team (captains)** — each captain sees a live **My Team** panel: their
   roster (players won + price paid), remaining budget, and a counter of how
   many players are still left to be auctioned.

5. **Results tab** (admin only) — sold / unsold players, final budgets, and
   CSV / JSON export. **Reset Tournament** wipes everything for a fresh start.

6. **Public viewer screen** — `/view` (e.g. `https://your-server/view`) is a
   no-login big-screen dashboard. It shows the live scoreboard (current player,
   highest bid, countdown), sold/unsold/remaining counters, and team rosters
   that update in real time. From the Live Auction tab the admin can toggle the
   viewer between the **live board** and a **Final Teams** display (full
   rosters + spent/left budget per captain).

7. **Equal team distribution** — once every captain's budget is exhausted, the
   admin can click **🎲 Distribute remaining players equally**. Every captain is
   filled up to the exact same squad size (`total players ÷ captains`); any
   remainder stays undistributed in the pool instead of pushing one team over
   the target (41 players ÷ 4 captains = exactly 10 each, 1 left over). On the
   Results tab the admin can also **manually assign** any leftover player to a
   chosen captain, and **return** a distributed (₹0) player to the pool to
   rebalance. Auction purchases are never changed — only leftover players.

## 4. API summary

| Method | Path                 | Role    | Purpose                                   |
|--------|----------------------|---------|-------------------------------------------|
| GET    | `/`                  | —       | The app UI                                |
| GET    | `/view`              | —       | Public no-login live viewer screen        |
| GET    | `/api/state`         | any     | Full state snapshot (polled every 1.5 s)  |
| POST   | `/api/login`         | —       | Sign in as admin or captain               |
| POST   | `/api/logout`        | any     | Sign out                                  |
| POST   | `/api/settings`      | admin   | Save rules, admin passcode, viewer mode   |
| POST   | `/api/captains`      | admin   | Add a captain                             |
| DELETE | `/api/captains/<id>` | admin   | Remove a captain                          |
| POST   | `/api/players`       | admin   | Add a player manually                     |
| DELETE | `/api/players/<id>`  | admin   | Remove a player                           |
| POST   | `/api/import`        | admin   | Upload CSV (multipart `file`)             |
| POST   | `/api/nominate`      | admin   | Nominate a player (`player_id`, incl. unsold rebid) or random |
| POST   | `/api/bid`           | captain | Place the next valid bid                  |
| POST   | `/api/sell`          | admin   | Sell / mark unsold (`sold` true|false)    |
| POST   | `/api/cancel`        | admin   | Abort current round, player stays free    |
| POST   | `/api/wishlist`      | captain | Toggle a player on/off the wishlist       |
| POST   | `/api/distribute_remaining` | admin | Balanced random distribution (exact per-team count) |
| POST   | `/api/assign_player` | admin   | Manually assign a leftover player to a captain |
| POST   | `/api/unassign_player` | admin  | Return a distributed (₹0) player to the pool |
| POST   | `/api/reset`         | admin   | Wipe the tournament                       |
| GET    | `/api/export.csv`    | admin   | Download results as CSV (incl. `unsold_count`) |
| GET    | `/api/export.json`   | admin   | Download full state as JSON               |

## 5. Notes & assumptions

- **Live updates:** clients poll `/api/state` every 1.5 s. Good enough for an
  auction and far simpler/more reliable than WebSockets. On each poll the
  server also lazily finalises a timer that expired in `timer` close mode.
- **Security:** designed for a trusted club network. Every captain needs a
  passcode to sign in; an admin passcode is strongly recommended so only the
  admin can configure the tournament. Passcodes are checked server-side but
  stored in plain text in `auction.db`. Consider HTTPS (nginx or Cloud Run) if
  you deploy over the public internet.
- **"Only admin can see":** Setup, Players, Results and Export are admin-only.
  Captains see the live auction board, their own wishlist, and their own **My
  Team** roster. The `/view` screen is deliberately public — it is meant for a
  projector/TV so spectators can watch without signing in.
- **Unsold tracking:** every "mark unsold" or timer-expiry-no-bid increments a
  player's `unsold_count`. The count is a permanent audit trail — it survives
  rebidding and even the player being sold later.
- **Timer expiry edge cases** (`admin` close mode, or `timer` mode when a
  captain disconnects): the admin always has **Sell / Mark Unsold / Cancel**
  buttons to close any round manually.
