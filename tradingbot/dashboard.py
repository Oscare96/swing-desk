"""Web dashboard and controls. Protect it with DASHBOARD_PASSWORD (Replit Secret).

Without a password the page is read-only and every control is refused.
/health is open (for an uptime monitor): 200 while the scheduler heartbeat is fresh, 503 if stale.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime

from flask import Flask, Response, redirect, render_template_string, request

from .gates import load_report, trading_unlocked
from .live import NY

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>Swing Desk</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--good:#1f7a4d;--bad:#b3261e;--warn:#9a6700;--accent:#2f5fd0}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mute:#9b9a94;--line:#33332f;--good:#5cc28d;--bad:#ff8a80;--warn:#e3b341;--accent:#8ab0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 12px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:0 0 10px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;overflow-x:auto}
.kpis{display:flex;flex-wrap:wrap;gap:18px}.kpi b{display:block;font-size:20px}.kpi span{color:var(--mute);font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--mute);font-weight:500}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.pill{display:inline-block;padding:2px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px;margin-right:6px}
form{display:inline}button{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;margin:0 6px 6px 0}
button.danger{border-color:var(--bad);color:var(--bad)}.mute{color:var(--mute)}svg{width:100%;height:160px}
</style></head><body><main>
<h1>Swing Desk <span class="pill">{{ 'PAPER' if paper else 'LIVE' }}</span></h1>
<div class="card" style="margin-bottom:12px">
 <span class="pill {{ 'good' if unlocked else 'bad' }}">{{ 'Backtest gate passed' if unlocked else 'Trading blocked' }}</span>
 <span class="pill {{ 'good' if regime_on else 'warn' }}">Regime {{ 'risk-on' if regime_on else 'risk-off' }}</span>
 {% if paused %}<span class="pill warn">Entries paused</span>{% endif %}
 {% if halted %}<span class="pill bad">Circuit breaker latched</span>{% endif %}
 <span class="pill {{ 'good' if hb_ok else 'bad' }}">Heartbeat {{ hb }}</span>
 {% if not unlocked %}<p class="bad">{{ gate_msg }}</p>{% endif %}
 {% if error %}<p class="bad">Broker error: {{ error }}</p>{% endif %}
</div>
<div class="grid">
<div class="card"><h2>Account</h2><div class="kpis">
 <div class="kpi"><b>${{ "{:,.0f}".format(equity) }}</b><span>Equity</span></div>
 <div class="kpi"><b class="{{ 'good' if day_pnl>=0 else 'bad' }}">{{ "{:+,.0f}".format(day_pnl) }}</b><span>Today</span></div>
 <div class="kpi"><b>${{ "{:,.0f}".format(desk_eq) }}</b><span>Desk (peak ${{ "{:,.0f}".format(desk_peak) }})</span></div>
</div>{{ chart|safe }}</div>
<div class="card"><h2>Controls</h2>
{% if controls %}
 <form method="post" action="/control/{{ 'resume' if paused else 'pause' }}"><button>{{ 'Resume entries' if paused else 'Pause new entries' }}</button></form>
 <form method="post" action="/control/cancel-entries"><button>Cancel pending entries</button></form>
 {% if halted %}<form method="post" action="/control/reset-breaker" onsubmit="return confirm('Reset the circuit breaker? Do this only after reviewing what went wrong.')"><button>Reset circuit breaker</button></form>{% endif %}
 <form method="post" action="/control/close-all" onsubmit="return prompt('Type FLATTEN to close every position')==='FLATTEN'"><input type="hidden" name="confirm" value="FLATTEN"><button class="danger">Close all positions</button></form>
{% else %}<p class="mute">Read-only: set DASHBOARD_PASSWORD to enable controls.</p>{% endif %}
 <p class="mute">Last run: {{ summary_text }}</p></div>
</div>
<div class="card" style="margin-top:12px"><h2>Positions</h2><table><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Price</th><th>P&L</th><th>Stop</th><th>Since</th></tr>
{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.qty}}</td><td>{{p.entry}}</td><td>{{p.price}}</td><td class="{{ 'good' if p.pl>=0 else 'bad' }}">{{ "{:+,.2f}".format(p.pl) }}</td><td>{{p.stop}}</td><td>{{p.since}}</td></tr>
{% else %}<tr><td colspan=7 class="mute">No positions</td></tr>{% endfor %}</table></div>
<div class="grid" style="margin-top:12px">
<div class="card"><h2>Open orders</h2><table><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Stop</th></tr>
{% for o in orders %}<tr><td>{{o.symbol}}</td><td>{{o.side}}</td><td>{{o.type}}</td><td>{{o.qty}}</td><td>{{o.stop_price or ''}}</td></tr>
{% else %}<tr><td colspan=5 class="mute">None</td></tr>{% endfor %}</table></div>
<div class="card"><h2>Backtest (out of sample)</h2>{% if report %}
<table>{% for c in report.checks %}<tr><td>{{c.check}}</td><td>{{c.value}}</td><td class="mute">{{c.rule}}</td><td class="{{ 'good' if c.passed else 'bad' }}">{{ 'pass' if c.passed else 'fail' }}</td></tr>{% endfor %}</table>
<p class="mute">Source {{report.data_source}}, run {{report.generated_at}}</p>{% else %}<p class="mute">No report yet.</p>{% endif %}</div>
</div>
<div class="card" style="margin-top:12px"><h2>Closed trades</h2><table><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th><th>R</th><th>Reason</th></tr>
{% for t in trades %}<tr><td>{{t.symbol}}</td><td>{{t.entry_date}}</td><td>{{t.exit_date}}</td><td class="{{ 'good' if (t.pnl or 0)>=0 else 'bad' }}">{{ "{:+,.2f}".format(t.pnl or 0) }}</td><td>{{ "%.2f"|format(t.r_multiple) if t.r_multiple is not none else '' }}</td><td>{{t.exit_reason}}</td></tr>
{% else %}<tr><td colspan=6 class="mute">None yet</td></tr>{% endfor %}</table>
<p class="mute">{{ trade_line }}</p></div>
<div class="card" style="margin-top:12px"><h2>Journal</h2><table><tr><th>Time (UTC)</th><th>Kind</th><th>Symbol</th><th>Message</th></tr>
{% for j in journal %}<tr><td>{{j.ts[5:16]}}</td><td>{{j.kind}}</td><td>{{j.symbol}}</td><td style="white-space:normal">{{j.message}}</td></tr>{% endfor %}</table></div>
</main></body></html>"""


def _chart(hist: list[dict]) -> str:
    if len(hist) < 2:
        return '<p class="mute">Equity chart appears after two trading days.</p>'
    eq = [h["equity"] for h in hist]
    spy = [h["spy_close"] for h in hist]
    series = [("var(--accent)", [e / eq[0] for e in eq])]
    if all(spy) and spy[0]:
        series.append(("var(--mute)", [s / spy[0] for s in spy]))
    vals = [v for _, s in series for v in s]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    w, h = 600, 160
    out = [f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="Equity vs SPY">']
    for color, s in series:
        pts = " ".join(f"{i * w / (len(s) - 1):.1f},{h - 8 - (v - lo) / rng * (h - 16):.1f}" for i, v in enumerate(s))
        out.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>')
    out.append("</svg><p class=\"mute\">Blue: account, grey: SPY (both rebased to 1)</p>")
    return "".join(out)


def create_app(desk, cfg) -> Flask:
    app = Flask(__name__)
    store = desk.store
    password = os.environ.get("DASHBOARD_PASSWORD", "")

    def authed() -> bool:
        if not password:
            return True
        a = request.authorization
        return bool(a and hmac.compare_digest(a.password or "", password))

    @app.before_request
    def guard():
        if request.path == "/health":
            return None
        if not authed():
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="desk"'})
        return None

    @app.get("/health")
    def health():
        hb = store.get("heartbeat")
        if not hb:
            return {"ok": False, "reason": "no heartbeat yet"}, 503
        age = (datetime.now(NY) - datetime.fromisoformat(hb)).total_seconds()
        ok = age < 180
        return {"ok": ok, "heartbeat_age_s": int(age)}, 200 if ok else 503

    @app.get("/")
    def index():
        error = None
        positions, orders, equity, day_pnl = [], [], 0.0, 0.0
        plans = store.plans()
        try:
            acct = desk.broker.account()
            equity = float(acct["equity"])
            day_pnl = equity - float(acct.get("last_equity") or equity)
            for sym, p in desk.broker.positions().items():
                plan = plans.get(sym, {})
                positions.append({"symbol": sym, "qty": p["qty"], "entry": f"{float(p['avg_entry_price']):.2f}",
                                  "price": f"{float(p['current_price']):.2f}", "pl": float(p["unrealized_pl"]),
                                  "stop": f"{plan['stop']:.2f}" if plan.get("stop") else ("core" if sym == cfg.regime.benchmark else "-"),
                                  "since": plan.get("entry_date", "")})
            orders = desk.broker.open_orders()
        except Exception as e:
            error = str(e)[:200]
        hb = store.get("heartbeat")
        hb_age = (datetime.now(NY) - datetime.fromisoformat(hb)).total_seconds() if hb else None
        trades = store.trades()
        if trades:
            wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
            gross_w = sum(t["pnl"] for t in trades if (t["pnl"] or 0) > 0)
            gross_l = -sum(t["pnl"] for t in trades if (t["pnl"] or 0) < 0)
            pf = f"{gross_w / gross_l:.2f}" if gross_l else "n/a"
            trade_line = f"{len(trades)} trades, win rate {wins / len(trades):.0%}, profit factor {pf}, total {sum(t['pnl'] or 0 for t in trades):+,.2f}"
        else:
            trade_line = ""
        unlocked, gate_msg = trading_unlocked(cfg)
        summary = store.get("last_summary")
        desk_eq = store.get("desk_peak", 0.0)
        try:
            desk_eq = desk._desk_equity(desk.broker.positions())
        except Exception:
            pass
        return render_template_string(
            PAGE, paper=desk.broker.paper, unlocked=unlocked, gate_msg=gate_msg,
            regime_on=bool(store.get("regime_on", False)), paused=bool(store.get("paused", False)),
            halted=bool(store.get("halted", False)), hb=f"{int(hb_age)}s ago" if hb_age is not None else "none",
            hb_ok=hb_age is not None and hb_age < 180, error=error, equity=equity, day_pnl=day_pnl,
            desk_eq=desk_eq, desk_peak=store.get("desk_peak", desk_eq), chart=_chart(store.equity_history()),
            controls=bool(password), summary_text=desk._summary_text(summary) if summary else "none yet",
            positions=positions, orders=orders, report=load_report(), trades=trades[:50],
            trade_line=trade_line, journal=store.journal(80))

    @app.post("/control/<action>")
    def control(action):
        if not password:
            return Response("Controls are disabled until DASHBOARD_PASSWORD is set.", 403)
        if action == "pause":
            desk.pause(True)
        elif action == "resume":
            desk.pause(False)
        elif action == "cancel-entries":
            desk.cancel_pending_entries()
        elif action == "reset-breaker":
            desk.reset_breaker()
        elif action == "close-all" and request.form.get("confirm") == "FLATTEN":
            desk.close_all()
        else:
            return Response("Unknown action", 400)
        return redirect("/")

    return app
