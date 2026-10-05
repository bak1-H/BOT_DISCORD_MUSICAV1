from types import SimpleNamespace

BOT_ID = 999
HUMAN_ID = 7
GUILD_ID = 1


class FakeBotUser:
    def __init__(self, user_id=BOT_ID):
        self.id = user_id


class FakeSender:
    def __init__(self, user_id=HUMAN_ID, is_bot=False, in_voice=True):
        self.id = user_id
        self.bot = is_bot
        self.voice = SimpleNamespace(channel=object()) if in_voice else None


class FakeTyping:
    def __init__(self, channel):
        self.channel = channel

    async def __aenter__(self):
        if self.channel.typing_error is not None:
            raise self.channel.typing_error
        self.channel.typing_entered += 1

    async def __aexit__(self, *exc_info):
        self.channel.typing_exited += 1


class FakeTextChannel:
    def __init__(self, channel_id=50):
        self.id = channel_id
        self.sent = []
        self.send_error = None
        self.typing_error = None
        self.typing_entered = 0
        self.typing_exited = 0

    def typing(self):
        return FakeTyping(self)

    async def send(self, content=None, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        sent = FakeSentMessage(content, kwargs)
        self.sent.append(sent)
        return sent


class FakeSentMessage:
    def __init__(self, content, kwargs):
        self.content = content
        self.kwargs = kwargs
        self.view = kwargs.get("view")
        self.edits = []
        self.edit_error = None

    async def edit(self, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)


class FakeIncomingMessage:
    def __init__(
        self,
        content,
        author=None,
        channel=None,
        mentions=(),
        reply_to=None,
        in_guild=True,
    ):
        self.content = content
        self.author = author or FakeSender()
        self.channel = channel or FakeTextChannel()
        self.mentions = list(mentions)
        self.guild = SimpleNamespace(id=GUILD_ID) if in_guild else None
        self.reference = SimpleNamespace(resolved=reply_to) if reply_to is not None else None
        self.reply_error = None
        self.replies = []

    async def reply(self, content=None, **kwargs):
        if self.reply_error is not None:
            raise self.reply_error
        sent = FakeSentMessage(content, kwargs)
        self.replies.append(sent)
        return sent

    @property
    def all_sent(self):
        return self.replies + self.channel.sent


class FakeResponse:
    def __init__(self):
        self.deferred = 0
        self.edited = []
        self.sent = []
        self.error = None

    async def defer(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.deferred += 1

    async def edit_message(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.edited.append(kwargs)

    async def send_message(self, content=None, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id=HUMAN_ID):
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.original_edits = []
        self.followups = []
        self.original_edit_error = None

    async def edit_original_response(self, **kwargs):
        if self.original_edit_error is not None:
            raise self.original_edit_error
        self.original_edits.append(kwargs)

    @property
    def followup(self):
        return SimpleNamespace(send=self._followup_send)

    async def _followup_send(self, content=None, **kwargs):
        self.followups.append((content, kwargs))
