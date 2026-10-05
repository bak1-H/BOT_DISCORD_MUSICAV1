import traceback

import discord

from agent.tools import execute_pending

CONFIRMATION_TIMEOUT_S = 60
NOT_YOUR_CONFIRMATION = "Solo quien hizo el pedido puede confirmar."
ALREADY_RESOLVED = "Esta confirmación ya fue resuelta."
CANCELLED = "Cancelado. No se ejecutó nada."
EXPIRED = "La confirmación expiró. No se ejecutó nada."
EXECUTION_FAILED = "No pude completar la acción."


class ConfirmView(discord.ui.View):
    def __init__(self, actions, ctx, author_id: int, execute=execute_pending, timeout: float = CONFIRMATION_TIMEOUT_S):
        super().__init__(timeout=timeout)
        self._actions = tuple(actions)
        self._ctx = ctx
        self._author_id = author_id
        self._execute = execute
        self._resolved = False
        self.message = None
        self._add_buttons()

    def _add_buttons(self) -> None:
        confirm_label = "Confirmar todo" if len(self._actions) > 1 else "Confirmar"
        confirm = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.danger)
        confirm.callback = self.handle_confirm
        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        cancel.callback = self.handle_cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id == self._author_id:
            return True
        await self._reply_privately(interaction, NOT_YOUR_CONFIRMATION)
        return False

    async def _reply_privately(self, interaction, text: str) -> None:
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except Exception as error:
            print(f"Confirm reply error: {error}")

    async def handle_confirm(self, interaction) -> None:
        if self._resolved:
            return await self._reply_privately(interaction, ALREADY_RESOLVED)
        self._resolved = True
        try:
            await interaction.response.defer()
        except Exception as error:
            print(f"Confirm defer error: {error}")
            self._resolved = False
            return
        self.stop()
        results = [await self._run(action) for action in self._actions]
        await self._publish(interaction, "\n".join(results))

    async def _run(self, action) -> str:
        try:
            return await self._execute(self._ctx, action)
        except Exception:
            traceback.print_exc()
            return EXECUTION_FAILED

    async def _publish(self, interaction, text: str) -> None:
        try:
            await interaction.edit_original_response(content=text, view=None)
            return
        except Exception as error:
            print(f"Confirm edit error: {error}")
        try:
            await interaction.followup.send(text)
        except Exception as error:
            print(f"Confirm followup error: {error}")

    async def handle_cancel(self, interaction) -> None:
        if self._resolved:
            return await self._reply_privately(interaction, ALREADY_RESOLVED)
        self._resolved = True
        self.stop()
        try:
            await interaction.response.edit_message(content=CANCELLED, view=None)
        except Exception as error:
            print(f"Confirm cancel error: {error}")

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        self.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(content=EXPIRED, view=None)
        except Exception as error:
            print(f"Confirm expiry edit error: {error}")
