import os

# Cluster configuration
NODES = {
    0: ("localhost", 5000),
    1: ("localhost", 5001),
    2: ("localhost", 5002),
    3: ("localhost", 5003),
    4: ("localhost", 5004),
    # 5: ("localhost", 5005),
    # 6: ("localhost", 5006),
    # 7: ("localhost", 5007),
}

# Timing (seconds)
ELECTION_TIMEOUT_MIN = 0.5
ELECTION_TIMEOUT_MAX = 1.0
HEARTBEAT_INTERVAL   = 0.05

# State file directory (for persistence)
STORAGE_DIR = os.path.join(os.path.dirname(__file__), "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)
