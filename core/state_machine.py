from enum import Enum, auto

# SYSTEM STATES


class SystemState(Enum):
    """
    Every state the system can be in.
    Only one state is active at a time.
    """
    STARTUP          = auto()  # initial boot — checking for crashes
    CRASH_RECOVERY   = auto()  # crashed session found — restoring state
    WAITING_OPERATOR = auto()  # idle — waiting for RFID/PIN login
    CALIBRATING      = auto()  # 2-minute baseline collection in progress
    MONITORING       = auto()  # active fatigue detection — normal operation
    FACE_LOSS        = auto()  # face not detected — PERCLOS paused
    TAMPER_ALERT     = auto()  # camera obstructed > 30s
    ALERT_L1         = auto()  # mild fatigue detected
    ALERT_L2         = auto()  # moderate fatigue detected
    ALERT_L3         = auto()  # critical fatigue — loud repeated alert
    SESSION_CLOSING  = auto()  # shift ending — generating PDF, cleaning up
    SHUTTING_DOWN    = auto()  # application closing — all threads exiting


# VALID TRANSITIONS


# Maps: current_state -> set of states it can transition TO
# If a transition is not in this map, it is INVALID.
VALID_TRANSITIONS = {

    SystemState.STARTUP: {
        SystemState.CRASH_RECOVERY,   # crashed session found on boot
        SystemState.WAITING_OPERATOR, # clean start
    },

    SystemState.CRASH_RECOVERY: {
        SystemState.MONITORING,       # checkpoint restored — resume monitoring
        SystemState.WAITING_OPERATOR, # recovery failed — back to login
    },

    SystemState.WAITING_OPERATOR: {
        SystemState.CALIBRATING,      # operator logged in
        SystemState.SHUTTING_DOWN,    # application closed at login screen
    },

    SystemState.CALIBRATING: {
        SystemState.MONITORING,       # calibration successful
        SystemState.WAITING_OPERATOR, # calibration failed / aborted
        SystemState.SHUTTING_DOWN,
    },

    SystemState.MONITORING: {
        SystemState.ALERT_L1,         # mild fatigue detected
        SystemState.ALERT_L2,         # skipped L1 — score jumped straight to L2
        SystemState.ALERT_L3,         # skipped — score jumped straight to L3
        SystemState.FACE_LOSS,        # face disappeared > 3 seconds
        SystemState.SESSION_CLOSING,  # shift end button or new RFID swipe
        SystemState.SHUTTING_DOWN,
    },

    SystemState.FACE_LOSS: {
        SystemState.MONITORING,       # face redetected
        SystemState.TAMPER_ALERT,     # face absent > 30s
        SystemState.SESSION_CLOSING,
        SystemState.SHUTTING_DOWN,
    },

    SystemState.TAMPER_ALERT: {
        SystemState.MONITORING,       # camera cleared — face redetected
        SystemState.SESSION_CLOSING,
        SystemState.SHUTTING_DOWN,
    },

    SystemState.ALERT_L1: {
        SystemState.MONITORING,       # operator acknowledged OR score dropped
        SystemState.ALERT_L2,         # not acknowledged in 30s OR score rose
        SystemState.SESSION_CLOSING,
        SystemState.SHUTTING_DOWN,
    },

    SystemState.ALERT_L2: {
        SystemState.MONITORING,       # acknowledged OR score dropped
        SystemState.ALERT_L3,         # not acknowledged in 30s OR score rose
        SystemState.SESSION_CLOSING,
        SystemState.SHUTTING_DOWN,
    },

    SystemState.ALERT_L3: {
        SystemState.MONITORING,       # acknowledged + score dropped below L1
        SystemState.SESSION_CLOSING,  # new RFID swipe during critical alert
        SystemState.SHUTTING_DOWN,
    },

    SystemState.SESSION_CLOSING: {
        SystemState.WAITING_OPERATOR, # session closed, PDF done, ready for next
        SystemState.CALIBRATING,      # new operator swiped in immediately
        SystemState.SHUTTING_DOWN,
    },

    SystemState.SHUTTING_DOWN: {
        # Terminal state — no transitions out
    },
}




class StateMachine:
    

    def __init__(self):
        self._state = SystemState.STARTUP
        self._history = [SystemState.STARTUP]

    @property
    def state(self) -> SystemState:
        return self._state

    def transition(self, new_state: SystemState) -> bool:
        
        allowed = VALID_TRANSITIONS.get(self._state, set())

        if new_state not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition from {self._state.name} "
                f"to {new_state.name}. "
                f"Allowed from {self._state.name}: "
                f"{[s.name for s in allowed]}"
            )

        old_state = self._state
        self._state = new_state
        self._history.append(new_state)
        print(f"[STATE] {old_state.name} → {new_state.name}")
        return True

    def can_transition(self, new_state: SystemState) -> bool:
       
        return new_state in VALID_TRANSITIONS.get(self._state, set())

    def get_allowed_transitions(self):
        """Returns list of valid states from current state."""
        return list(VALID_TRANSITIONS.get(self._state, set()))

    def is_alert_state(self) -> bool:
        """Returns True if currently in any alert state."""
        return self._state in {
            SystemState.ALERT_L1,
            SystemState.ALERT_L2,
            SystemState.ALERT_L3
        }

    def is_monitoring_active(self) -> bool:
        """
        Returns True if PERCLOS engine should be running.
        False during calibration, login, shutdown.
        """
        return self._state in {
            SystemState.MONITORING,
            SystemState.ALERT_L1,
            SystemState.ALERT_L2,
            SystemState.ALERT_L3,
        }

    def is_face_loss(self) -> bool:
        """Returns True if in FACE_LOSS or TAMPER_ALERT state."""
        return self._state in {
            SystemState.FACE_LOSS,
            SystemState.TAMPER_ALERT
        }

    @property
    def history(self):
        """Returns full state transition history for debugging."""
        return self._history.copy()

    def __repr__(self):
        return f"StateMachine(state={self._state.name})"


# CUSTOM EXCEPTION
# Raised when an invalid state transition is attempted.

class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass




if __name__ == "__main__":
    print("=" * 50)
    print("State machine self-test")
    print("=" * 50)

    sm = StateMachine()
    print(f"Initial state: {sm.state.name}")

    # Test valid transitions
    tests_pass = 0
    tests_fail = 0

    def test_valid(from_s, to_s):
        global tests_pass
        try:
            sm._state = from_s
            sm.transition(to_s)
            print(f"   {from_s.name} → {to_s.name}")
            tests_pass += 1
        except InvalidTransitionError as e:
            print(f"   FAILED: {e}")
            tests_fail += 1

    def test_invalid(from_s, to_s):
        global tests_pass
        try:
            sm._state = from_s
            sm.transition(to_s)
            print(f"  Should have FAILED: {from_s.name} → {to_s.name}")
            tests_fail += 1
        except InvalidTransitionError:
            print(f" Correctly blocked: {from_s.name} → {to_s.name}")
            tests_pass += 1

    print("\nValid transitions:")
    test_valid(SystemState.STARTUP,          SystemState.WAITING_OPERATOR)
    test_valid(SystemState.WAITING_OPERATOR, SystemState.CALIBRATING)
    test_valid(SystemState.CALIBRATING,      SystemState.MONITORING)
    test_valid(SystemState.MONITORING,       SystemState.ALERT_L1)
    test_valid(SystemState.ALERT_L1,         SystemState.ALERT_L2)
    test_valid(SystemState.ALERT_L2,         SystemState.ALERT_L3)
    test_valid(SystemState.ALERT_L3,         SystemState.MONITORING)
    test_valid(SystemState.MONITORING,       SystemState.FACE_LOSS)
    test_valid(SystemState.FACE_LOSS,        SystemState.TAMPER_ALERT)
    test_valid(SystemState.MONITORING,       SystemState.SESSION_CLOSING)
    test_valid(SystemState.SESSION_CLOSING,  SystemState.WAITING_OPERATOR)

    print("\nInvalid transitions (must be blocked):")
    test_invalid(SystemState.MONITORING,       SystemState.STARTUP)
    test_invalid(SystemState.ALERT_L1,         SystemState.CALIBRATING)
    test_invalid(SystemState.WAITING_OPERATOR, SystemState.ALERT_L2)
    test_invalid(SystemState.SHUTTING_DOWN,    SystemState.MONITORING)

    print(f"\nResults: {tests_pass} passed, {tests_fail} failed")
    if tests_fail == 0:
        print("State machine is working correctly.")
    else:
        print(" Fix the failures before proceeding.")