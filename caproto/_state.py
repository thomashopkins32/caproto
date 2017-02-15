from ._commands import *
from ._utils import *


COMMAND_TRIGGERED_CIRCUIT_TRANSITIONS = {
    CLIENT: {
        SEND_VERSION_REQUEST: {
            EchoRequest: SEND_VERSION_REQUEST,
            EchoResponse: SEND_VERSION_REQUEST,
            VersionRequest: AWAIT_VERSION_RESPONSE,
            ErrorResponse: ERROR,
        },
        AWAIT_VERSION_RESPONSE: {
            EchoRequest: AWAIT_VERSION_RESPONSE,
            EchoResponse: AWAIT_VERSION_RESPONSE,
            # Host and Client requests may come before or after we connect.
            HostNameRequest: AWAIT_VERSION_RESPONSE,
            ClientNameRequest: AWAIT_VERSION_RESPONSE,
            VersionResponse: CONNECTED,
            ErrorResponse: ERROR, },
        CONNECTED: {
            EchoRequest: CONNECTED,
            EchoResponse: CONNECTED,
            # Host and Client requests may come before or after we connect.
            HostNameRequest: CONNECTED,
            ClientNameRequest: CONNECTED,
            AccessRightsResponse: CONNECTED,
            ErrorResponse: ERROR,
            # VirtualCircuits can only be closed by timeout.
        },
        ERROR: {},
    },
    SERVER: {
        IDLE: {
            VersionRequest: SEND_VERSION_RESPONSE,
            EchoRequest: IDLE,
            EchoResponse: IDLE,
            ErrorResponse: ERROR,
        },
        SEND_VERSION_RESPONSE: {
            VersionResponse: CONNECTED,
            EchoRequest: SEND_VERSION_RESPONSE,
            EchoResponse: SEND_VERSION_RESPONSE,
            # Host and Client requests may come before or after we connect.
            HostNameRequest: SEND_VERSION_RESPONSE,
            ClientNameRequest: SEND_VERSION_RESPONSE,
            ErrorResponse: ERROR,
        },
        CONNECTED: {
            # Host and Client requests may come before or after we connect.
            HostNameRequest: CONNECTED,
            ClientNameRequest: CONNECTED,
            AccessRightsResponse: CONNECTED,
            EchoRequest: CONNECTED,
            EchoResponse: CONNECTED,
            ErrorResponse: ERROR,
            # VirtualCircuits can only be closed by timeout.
        },
    },
}


COMMAND_TRIGGERED_CHANNEL_TRANSITIONS = {
    CLIENT: {
        SEND_CREATE_CHAN_REQUEST: {
            CreateChanRequest: AWAIT_CREATE_CHAN_RESPONSE,
            ErrorResponse: ERROR,
        },
        AWAIT_CREATE_CHAN_RESPONSE: {
            CreateChanResponse: CONNECTED,
            ErrorResponse: ERROR,
        },
        CONNECTED: {
            ReadNotifyRequest: CONNECTED,
            WriteNotifyRequest: CONNECTED,
            ReadNotifyResponse: CONNECTED,
            WriteNotifyResponse: CONNECTED,
            EventAddRequest: CONNECTED,
            EventCancelRequest: CONNECTED,
            EventAddResponse: CONNECTED,
            EventCancelResponse: CONNECTED,
            ClearChannelRequest: MUST_CLOSE,
            ServerDisconnResponse: CLOSED,
            ErrorResponse: ERROR,
        },
        MUST_CLOSE: {
            ClearChannelResponse: CLOSED,
            ServerDisconnResponse: CLOSED,
            ErrorResponse: ERROR,
        },
        CLOSED: {
            # a terminal state
        },
        ERROR: {
            # a terminal state
        },
    },
    SERVER: {
        IDLE: {
            CreateChanRequest: SEND_CREATE_CHAN_RESPONSE,
            ErrorResponse: ERROR,
        },
        SEND_CREATE_CHAN_RESPONSE: {
            CreateChanResponse: CONNECTED,
            ErrorResponse: ERROR,
        },
        CONNECTED: {
            ReadNotifyRequest: CONNECTED,
            WriteNotifyRequest: CONNECTED,
            ReadNotifyResponse: CONNECTED,
            WriteNotifyResponse: CONNECTED,
            EventAddRequest: CONNECTED,
            EventCancelRequest: CONNECTED,
            EventAddResponse: CONNECTED,
            EventCancelResponse: CONNECTED,
            ClearChannelRequest: MUST_CLOSE,
            ServerDisconnResponse: CLOSED,
            ErrorResponse: ERROR,
        },
        MUST_CLOSE: {
            ClearChannelResponse: CLOSED,
            ServerDisconnResponse: CLOSED,
            ErrorResponse: ERROR,
        },
        CLOSED: {
            # a terminal state
        },
        ERROR: {
            # a terminal state
        },
    },
}

STATE_TRIGGERED_TRANSITIONS = {
    # (CHANNEL_STATE, CIRCUIT_STATE)
    CLIENT: {
        (NEED_CIRCUIT, CONNECTED): (SEND_CREATE_CHAN_REQUEST, CONNECTED),
    },
    SERVER: {
        (NEED_CIRCUIT, CONNECTED): (SEND_CREATE_CHAN_REQUEST, CONNECTED),
    }
}


class _BaseState:
    def __repr__(self):
        return "<{!s} states={!r}>".format(type(self).__name__, self.states)

    def _fire_command_triggered_transitions(self, role, command_type):
        state = self.states[role]
        try:
            new_state = self.TRANSITIONS[role][state][command_type]
        except KeyError:
            err =  get_exception(role, command_type)
            raise LocalProtocolError(
                "{} cannot handle command type {} when role={} and state={}"
                .format(self, command_type.__name__, role, self.states[role]))
        self.states[role] = new_state


class ChannelState(_BaseState):
    TRANSITIONS = COMMAND_TRIGGERED_CHANNEL_TRANSITIONS
    STT = STATE_TRIGGERED_TRANSITIONS

    def __init__(self):
        self.states = {CLIENT: SEND_CREATE_CHAN_REQUEST, SERVER: IDLE}
        # Same pattern as in _BaseChannel: the VirtualCircuitProxy gets bound
        # exactly once later.
        self._circuit_state = None

    @property
    def circuit_state(self):
        return self._circuit_state

    @circuit_state.setter
    def circuit_state(self, circuit_state):
        if self._circuit_state is not None:
            raise RuntimeError("circuit_state can only be set once")
        self._circuit_state = circuit_state

    def _fire_state_triggered_transitions(self, role):
        if self.circuit_state is None:
            # No circuit yet -- nothing to do.
            return
            new = self.STT[role].get((self.states[role],
                                        self.circuit_state.states[role]))
            if new is not None:
                self.states[role], circuit_state.states[role] = new

    def process_command(self, role, command_type):
        self._fire_command_triggered_transitions(role, command_type)
        self._fire_state_triggered_transitions(role)


class CircuitState(_BaseState):
    TRANSITIONS = COMMAND_TRIGGERED_CIRCUIT_TRANSITIONS

    def __init__(self):
        self.states = {CLIENT: SEND_VERSION_REQUEST, SERVER: IDLE}
        self._channels = None

    @property
    def channels(self):
        return self._channels

    @channels.setter
    def channels(self, channels):
        if self._channels is not None:
            raise RuntimeError("channels can only be set once")
        self._channels = channels

    def process_command(self, role, command_type):
        self._fire_command_triggered_transitions(role, command_type)
        for chan in self.channels.values():
            chan._state._fire_state_triggered_transitions(role)
        print(self)


def get_exception(our_role, command):
    """
    Return a (Local|Remote)ProtocolError depending on which command this is and
    which role we are playing.

    Note that this method does not raise; it is up to the caller to raise.

    Parameters
    ----------
    our_role: ``CLIENT`` or ``SERVER``
    command : Message instance or class
        We will test whether it is a ``REQUEST`` or ``RESPONSE``.
    """
    # TO DO Give commands an attribute so we can easily check whether one
    # is a Request or a Response
    if command.DIRECTION is REQUEST:
        party_at_fault = CLIENT
    elif command.DIRECTION is RESPONSE:
        party_at_fault = SERVER
    if our_role is party_at_fault:
        _class = LocalProtocolError
    else:
        _class =  RemoteProtocolError
    return _class
