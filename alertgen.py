"""
Synthetic alert generator for the Agentic Incident Triage project.
Deterministic given a seed: every student gets the same 200 alerts.
"""
import random, json
from datetime import datetime, timedelta

SERVICES = ["checkout-api", "auth-svc", "search-index", "payments-worker",
            "notify-gateway", "user-profile", "cart-cache", "recs-engine"]

# (label, weight, error-rate range, log templates)
SCENARIOS = [
    ("noise", 0.34, (0.001, 0.020), [
        "GET /healthz 200 in 3ms",
        "scheduled cache refresh complete",
        "connection pool resized 8 -> 10",
        "WARN slow query 142ms on users.email",
        "INFO gc pause 21ms",
    ]),
    ("degraded", 0.30, (0.012, 0.260), [
        "WARN upstream {dep} p99 latency 1840ms",
        "WARN retry budget 62% consumed",
        "WARN thread pool queue depth 88",
        "ERROR timeout calling {dep} after 3000ms",
        "WARN circuit breaker half-open for {dep}",
    ]),
    ("bad_deploy", 0.22, (0.018, 0.430), [
        "ERROR NullPointerException at OrderMapper.map(OrderMapper.java:88)",
        "ERROR 500 POST /v2/orders",
        "ERROR schema mismatch: column 'discount_pct' not found",
        "ERROR failed to deserialize response from {dep}",
        "FATAL unhandled exception in request handler",
    ]),
    ("dependency", 0.14, (0.020, 0.400), [
        "ERROR connection refused to {dep}:5432",
        "ERROR {dep} returned 503 Service Unavailable",
        "ERROR DNS resolution failed for {dep}",
        "ERROR TLS handshake timeout to {dep}",
    ]),
]
DEPS = ["postgres-primary", "redis-cart", "kafka-events", "s3-assets", "stripe-api"]


def _pick(rng):
    r, acc = rng.random(), 0.0
    for name, w, er, logs in SCENARIOS:
        acc += w
        if r <= acc:
            return name, er, logs
    return SCENARIOS[-1][0], SCENARIOS[-1][2], SCENARIOS[-1][3]


def generate_alerts(n=200, seed=0):
    """Return n alerts. Each dict is what your agent sees, plus hidden ground truth."""
    rng = random.Random(seed)
    t0 = datetime(2026, 3, 2, 9, 0)
    out = []
    for i in range(n):
        label, er_range, templates = _pick(rng)
        svc = rng.choice(SERVICES)
        dep = rng.choice(DEPS)
        ts = t0 + timedelta(minutes=17 * i + rng.randint(0, 9))

        # a bad_deploy is almost always minutes after a deploy; others rarely are
        if label == "bad_deploy":
            deploy_min_ago = (rng.randint(2, 20) if rng.random() < 0.75
                              else rng.randint(200, 3000))   # not every bad deploy is recent
        elif rng.random() < 0.30:
            deploy_min_ago = rng.randint(3, 25)              # coincidence: the trap
        else:
            deploy_min_ago = rng.randint(180, 4000)

        err = round(rng.uniform(*er_range), 4)
        n_lines = rng.randint(4, 9)
        logs = [rng.choice(templates).format(dep=dep) for _ in range(n_lines)]
        if label != "noise":                              # bury signal in noise
            logs += [rng.choice(SCENARIOS[0][3]) for _ in range(rng.randint(1, 4))]
        rng.shuffle(logs)

        out.append({
            "alert_id":   f"ALRT-{i:04d}",
            "service":    svc,
            "timestamp":  ts.isoformat(),
            "trigger":    f"error_rate above threshold on {svc}",
            "_truth": {                                   # hidden: for eval only
                "label": label,
                "correct_action": {"noise": "ignore",
                                   "degraded": "escalate",
                                   "bad_deploy": "rollback",
                                   "dependency": "escalate"}[label],
                "error_rate": err,
                "deploy_min_ago": deploy_min_ago,
                "dep": dep,
                "logs": logs,
            },
        })
    return out


# ---------- the three tools your agent may call ----------
class Tools:
    """Tools see the hidden truth. Your agent must not."""
    def __init__(self, alerts, seed=0, flaky=0.10):
        self._by_id = {a["alert_id"]: a for a in alerts}
        self._rng = random.Random(seed + 999)
        self.flaky = flaky
        self.calls = 0

    def _maybe_fail(self, name):
        self.calls += 1
        if self._rng.random() < self.flaky:
            raise TimeoutError(f"{name} timed out")

    def read_logs(self, alert_id, n=6):
        """Recent log lines for the alerting service."""
        self._maybe_fail("read_logs")
        return self._by_id[alert_id]["_truth"]["logs"][:n]

    def deploy_history(self, alert_id):
        """When did this service last deploy?"""
        self._maybe_fail("deploy_history")
        t = self._by_id[alert_id]["_truth"]
        return {"minutes_since_deploy": t["deploy_min_ago"],
                "last_deploy_sha": f"{abs(hash(alert_id)) % 0xfffffff:07x}"}

    def error_rate(self, alert_id):
        """Current error rate vs. the 7-day baseline."""
        self._maybe_fail("error_rate")
        t = self._by_id[alert_id]["_truth"]
        return {"current": t["error_rate"], "baseline_7d": 0.004}


ACTIONS = ["ignore", "escalate", "rollback"]

# cost of taking action A when the truth wanted action B  (dollars)
COST = {
    ("ignore",   "ignore"):   0.0,   ("ignore",   "escalate"): 400.0,
    ("ignore",   "rollback"): 900.0,
    ("escalate", "ignore"):    25.0, ("escalate", "escalate"):  25.0,
    ("escalate", "rollback"): 150.0,
    ("rollback", "ignore"):   600.0, ("rollback", "escalate"): 300.0,
    ("rollback", "rollback"):  60.0,
}

def score(alerts, decisions):
    """decisions: {alert_id: action}. Returns accuracy and mean cost per alert."""
    hit = total = 0.0
    for a in alerts:
        want = a["_truth"]["correct_action"]
        got = decisions.get(a["alert_id"], "ignore")
        hit += (got == want)
        total += COST[(got, want)]
    n = len(alerts)
    return {"accuracy": hit / n, "cost_per_alert": total / n, "n": n}


if __name__ == "__main__":
    alerts = generate_alerts(200, seed=0)
    from collections import Counter
    print(Counter(a["_truth"]["label"] for a in alerts))
    print(json.dumps({k: v for k, v in alerts[0].items() if k != "_truth"}, indent=2))
    t = Tools(alerts, flaky=0.0)
    print(t.read_logs("ALRT-0000", 3))
    print(t.deploy_history("ALRT-0000"), t.error_rate("ALRT-0000"))
    # always-escalate baseline
    print(score(alerts, {a["alert_id"]: "escalate" for a in alerts}))
    print(score(alerts, {a["alert_id"]: "ignore" for a in alerts}))
