import six
import weakref
import inflection

from collections import deque, namedtuple
from gevent.event import Event

from disco.types.base import UNSET
from disco.util.config import Config
from disco.util.hashmap import HashMap, DefaultHashMap


class StackMessage(namedtuple('StackMessage', ['id', 'channel_id', 'author_id'])):
    """
    A message stored on a stack inside of the state object, used for tracking
    previously sent messages in channels.

    Attributes
    ---------
    id : snowflake
        the id of the message
    channel_id : snowflake
        the id of the channel this message was sent in
    author_id : snowflake
        the id of the author of this message
    """


class StateConfig(Config):
    """
    A configuration object for determining how the State tracking behaves.

    Attributes
    ----------
    track_messages : bool
        Whether the state store should keep a buffer of previously sent messages.
        Message tracking allows for multiple higher-level shortcuts and can be
        highly useful when developing bots that need to delete their own messages.

        Message tracking is implemented using a deque and a namedtuple, meaning
        it should generally not have a high impact on memory, however users who
        find they do not need and may be experiencing memory pressure can disable
        this feature entirely using this attribute.
    track_messages_size : int
        The size of the deque for each channel. Using this you can calculate the
        total number of possible :class:`StackMessage` objects kept in memory,
        using: `total_mesages_size * total_channels`. This can be tweaked based
        on usage to help prevent memory pressure.
    sync_guild_members : bool
        If true, guilds will be automatically synced when they are initially loaded
        or joined. Generally this setting is OK for smaller bots, however bots in over
        50 guilds will notice this operation can take a while to complete.
    """
    track_messages = True
    track_messages_size = 100

    sync_guild_members = True


class State(object):
    """
    The State class is used to track global state based on events emitted from
    the :class:`GatewayClient`. State tracking is a core component of the Disco
    client, providing the mechanism for most of the higher-level utility functions.

    Attributes
    ----------
    EVENTS : list(str)
        A list of all events the State object binds to
    client : :class:`disco.client.Client`
        The Client instance this state is attached to
    config : :class:`StateConfig`
        The configuration for this state instance
    me : :class:`disco.types.user.User`
        The currently logged in user
    dms : dict(snowflake, :class:`disco.types.channel.Channel`)
        Mapping of all known DM Channels
    guilds : dict(snowflake, :class:`disco.types.guild.Guild`)
        Mapping of all known/loaded Guilds
    channels : dict(snowflake, :class:`disco.types.channel.Channel`)
        Weak mapping of all known/loaded Channels
    users : dict(snowflake, :class:`disco.types.user.User`)
        Weak mapping of all known/loaded Users
    voice_states : dict(str, :class:`disco.types.voice.VoiceState`)
        Weak mapping of all known/active Voice States
    messages : Optional[dict(snowflake, :class:`deque`)]
        Mapping of channel ids to deques containing :class:`StackMessage` objects
    """
    EVENTS = [
        'Ready', 'GuildCreate', 'GuildUpdate', 'GuildDelete', 'GuildMemberAdd', 'GuildMemberRemove',
        'GuildMemberUpdate', 'GuildMembersChunk', 'GuildRoleCreate', 'GuildRoleUpdate', 'GuildRoleDelete',
        'GuildEmojisUpdate', 'ChannelCreate', 'ChannelUpdate', 'ChannelDelete', 'VoiceStateUpdate', 'MessageCreate',
        'PresenceUpdate'
    ]

    def __init__(self, client, config):
        self.client = client
        self.config = config

        self.ready = Event()
        self.guilds_waiting_sync = 0

        self.me = None
        self.dms = HashMap()
        self.guilds = HashMap()
        self.channels = HashMap(weakref.WeakValueDictionary())
        self.users = HashMap(weakref.WeakValueDictionary())
        self.voice_states = HashMap(weakref.WeakValueDictionary())

        # If message tracking is enabled, listen to those events
        if self.config.track_messages:
            self.messages = DefaultHashMap(lambda: deque(maxlen=self.config.track_messages_size))
            self.EVENTS += ['MessageDelete', 'MessageDeleteBulk']

        # The bound listener objects
        self.listeners = []
        self.bind()

    def unbind(self):
        """
        Unbinds all bound event listeners for this state object.
        """
        map(lambda k: k.unbind(), self.listeners)
        self.listeners = []

    def bind(self):
        """
        Binds all events for this state object, storing the listeners for later
        unbinding.
        """
        assert not len(self.listeners), 'Binding while already bound is dangerous'

        for event in self.EVENTS:
            func = 'on_' + inflection.underscore(event)
            self.listeners.append(self.client.events.on(event, getattr(self, func)))

    def fill_messages(self, channel):
        for message in reversed(next(channel.messages_iter(bulk=True))):
            self.messages[channel.id].append(
                StackMessage(message.id, message.channel_id, message.author.id))

    def on_ready(self, event):
        self.me = event.user
        self.guilds_waiting_sync = len(event.guilds)

        for dm in event.private_channels:
            self.dms[dm.id] = dm
            self.channels[dm.id] = dm

    def on_message_create(self, event):
        if self.config.track_messages:
            self.messages[event.message.channel_id].append(
                StackMessage(event.message.id, event.message.channel_id, event.message.author.id))

        if event.message.channel_id in self.channels:
            self.channels[event.message.channel_id].last_message_id = event.message.id

    def on_message_delete(self, event):
        if event.channel_id not in self.messages:
            return

        sm = next((i for i in self.messages[event.channel_id] if i.id == event.id), None)
        if not sm:
            return

        self.messages[event.channel_id].remove(sm)

    def on_message_delete_bulk(self, event):
        if event.channel_id not in self.messages:
            return

        # TODO: performance
        for sm in list(self.messages[event.channel_id]):
            if sm.id in event.ids:
                self.messages[event.channel_id].remove(sm)

    def on_guild_create(self, event):
        if event.unavailable is False:
            self.guilds_waiting_sync -= 1
            if self.guilds_waiting_sync <= 0:
                self.ready.set()

        self.guilds[event.guild.id] = event.guild
        self.channels.update(event.guild.channels)

        for member in six.itervalues(event.guild.members):
            self.users[member.user.id] = member.user

        for voice_state in six.itervalues(event.guild.voice_states):
            self.voice_states[voice_state.session_id] = voice_state

        if self.config.sync_guild_members:
            event.guild.sync()

    def on_guild_update(self, event):
        self.guilds[event.guild.id].update(event.guild, ignored=[
            'channels',
            'members',
            'voice_states',
            'presences'
        ])

    def on_guild_delete(self, event):
        if event.id in self.guilds:
            # Just delete the guild, channel references will fall
            del self.guilds[event.id]

    def on_channel_create(self, event):
        if event.channel.is_guild and event.channel.guild_id in self.guilds:
            self.guilds[event.channel.guild_id].channels[event.channel.id] = event.channel
            self.channels[event.channel.id] = event.channel
        elif event.channel.is_dm:
            self.dms[event.channel.id] = event.channel
            self.channels[event.channel.id] = event.channel

    def on_channel_update(self, event):
        if event.channel.id in self.channels:
            self.channels[event.channel.id].update(event.channel)

            if event.overwrites is not UNSET:
                self.channels[event.channel.id].overwrites = event.overwrites
                self.channels[event.channel.id].after_load()

    def on_channel_delete(self, event):
        if event.channel.is_guild and event.channel.guild and event.channel.id in event.channel.guild.channels:
            del event.channel.guild.channels[event.channel.id]
        elif event.channel.is_dm and event.channel.id in self.dms:
            del self.dms[event.channel.id]

    def on_voice_state_update(self, event):
        # Existing connection, we are either moving channels or disconnecting
        if event.state.session_id in self.voice_states:
            # Moving channels
            if event.state.channel_id:
                self.voice_states[event.state.session_id].update(event.state)
            # Disconnection
            else:
                if event.state.guild_id in self.guilds:
                    if event.state.session_id in self.guilds[event.state.guild_id].voice_states:
                        del self.guilds[event.state.guild_id].voice_states[event.state.session_id]
                del self.voice_states[event.state.session_id]
        # New connection
        elif event.state.channel_id:
            if event.state.guild_id in self.guilds:
                self.guilds[event.state.guild_id].voice_states[event.state.session_id] = event.state
            self.voice_states[event.state.session_id] = event.state

    def on_guild_member_add(self, event):
        if event.member.user.id not in self.users:
            self.users[event.member.user.id] = event.member.user
        else:
            event.member.user = self.users[event.member.user.id]

        if event.member.guild_id not in self.guilds:
            return

        self.guilds[event.member.guild_id].members[event.member.id] = event.member

    def on_guild_member_update(self, event):
        if event.member.guild_id not in self.guilds:
            return

        if event.member.id not in self.guilds[event.member.guild_id].members:
            return

        self.guilds[event.member.guild_id].members[event.member.id].update(event.member)

    def on_guild_member_remove(self, event):
        if event.guild_id not in self.guilds:
            return

        if event.user.id not in self.guilds[event.guild_id].members:
            return

        del self.guilds[event.guild_id].members[event.user.id]

    def on_guild_members_chunk(self, event):
        if event.guild_id not in self.guilds:
            return

        guild = self.guilds[event.guild_id]
        for member in event.members:
            member.guild_id = guild.id
            guild.members[member.id] = member
            self.users[member.id] = member.user

    def on_guild_role_create(self, event):
        if event.guild_id not in self.guilds:
            return

        self.guilds[event.guild_id].roles[event.role.id] = event.role

    def on_guild_role_update(self, event):
        if event.guild_id not in self.guilds:
            return

        self.guilds[event.guild_id].roles[event.role.id].update(event.role)

    def on_guild_role_delete(self, event):
        if event.guild_id not in self.guilds:
            return

        if event.role_id not in self.guilds[event.guild_id].roles:
            return

        del self.guilds[event.guild_id].roles[event.role_id]

    def on_guild_emojis_update(self, event):
        if event.guild_id not in self.guilds:
            return

        self.guilds[event.guild_id].emojis = HashMap({i.id: i for i in event.emojis})

    def on_presence_update(self, event):
        if event.user.id in self.users:
            self.users[event.user.id].update(event.presence.user)
            self.users[event.user.id].presence = event.presence
            event.presence.user = self.users[event.user.id]

        if event.guild_id not in self.guilds:
            return

        if event.user.id not in self.guilds[event.guild_id].members:
            return

        self.guilds[event.guild_id].members[event.user.id].user.update(event.user)
