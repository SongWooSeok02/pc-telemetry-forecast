"""Agent clock decision state; inputs are recorded milliseconds, never corrected UTC."""
from verify_contract import measure


class ClockState:
    def __init__(self):
        self.generation = 0
        self.events = []
        self.reset()

    def reset(self):
        self.generation += 1
        self.pending = None
        self.baseline = None
        self.exceeded = 0
        self.status = 'unknown'

    def resume(self):
        self.events.append('resume')
        self.reset()

    def request(self, request_id, wall, mono):
        self.pending = (request_id, self.generation, wall, mono)

    def timeout(self):
        self.pending = None
        self.exceeded = 0
        self.status = 'unknown'

    def response(self, request_id, t2, t3, wall, mono, synced=True):
        if self.pending is None or self.pending[0] != request_id:
            return None  # Discarded response must not change a newer decision.
        _, generation, t1, m1 = self.pending
        self.pending = None
        if generation != self.generation:
            return None
        if mono-m1 >= 2000:
            self.timeout()
            return None
        if abs(wall-t1-(mono-m1)) >= 2000:
            self.events.append('clock_jump')
            self.reset()
            return None
        value = measure(t1, t2, t3, wall, m1, mono, synced=synced)
        if value is None:
            self.timeout()
            return None
        self.baseline = (wall, mono)
        self.exceeded = self.exceeded+1 if abs(value[0]) > 5000 else 0
        self.status = 'clock_skew_suspected' if self.exceeded >= 3 else 'normal' if not self.exceeded else 'unknown'
        return value

    def observe(self, wall, mono):
        if self.baseline is not None:
            old_wall, old_mono = self.baseline
            if abs(wall-old_wall-(mono-old_mono)) >= 2000:
                self.events.append('clock_jump')
                self.reset()
                return
        self.baseline = (wall, mono)
