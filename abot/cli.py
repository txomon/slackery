# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, unicode_literals

import asyncio
import click
import click._unicodefun  # type: ignore
import click.core
import click.utils
import logging
import shlex

# Allow type checking for circular dependencies
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import abot

logger = logging.getLogger(__name__)

tbd_tasks = []


class ExitCode(Exception):
    def __init__(self, code):
        super().__init__()
        self.code = code

    def __repr__(self):
        return f'ExitCode(code={self.code})'


class Context(click.Context):
    async def async_invoke(*args, **kwargs):
        self, callback = args[:2]
        ctx = self

        # Deleted unused code from super class

        args = args[2:]
        with click.core.augment_usage_errors(self):
            with ctx:
                return await callback(*args, **kwargs)

    def exit(self, code=0):
        raise ExitCode(code=code)


class AsyncCommandMixin:
    def invoke(self, ctx):
        """Given a context, this invokes the attached callback (if it exists)
        in the right way.
        """
        if self.callback is not None:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self.async_invoke(ctx))

    async def async_invoke(self, ctx):
        if self.callback is not None:
            return await ctx.async_invoke(self.callback, **ctx.params)

    def make_context(self, info_name, args, parent=None, **extra):
        for key, value in self.context_settings.items():
            if key not in extra:
                extra[key] = value
        ctx = Context(self, info_name=info_name, parent=parent, **extra)
        with ctx.scope(cleanup=False):
            self.parse_args(ctx, args)
        return ctx

    def get_help_option(self, ctx):
        """Returns the help option object."""
        help_options = self.get_help_option_names(ctx)
        if not help_options or not self.add_help_option:
            return

        def show_help(ctx, param, value):
            import abot.bot
            if value and not ctx.resilient_parsing:
                event: abot.bot.MessageEvent = abot.bot.current_event.get()
                tbd_tasks.append(event.reply(ctx.get_help()))
                ctx.exit()

        return click.core.Option(help_options, is_flag=True,
                                 is_eager=True, expose_value=False,
                                 callback=show_help,
                                 help='Show this message and exit.')


class Command(AsyncCommandMixin, click.Command):
    pass


class AsyncMultiCommandMixin(AsyncCommandMixin):
    def invoke(self, ctx):
        args = ctx.protected_args + ctx.args
        _, cmd, _ = self.resolve_command(ctx, args)
        if ctx.__class__ != Context or isinstance(cmd, click.Command):
            click.MultiCommand.invoke(self, ctx)
        else:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self.async_invoke(ctx))

    async def async_invoke(self, ctx):
        async def _process_result(value):
            if self.result_callback is not None:
                value = await ctx.async_invoke(self.result_callback, value,
                                               **ctx.params)
            return value

        if not ctx.protected_args:
            if self.invoke_without_command:
                if not self.chain:
                    return await Command.async_invoke(self, ctx)
                with ctx:
                    await Command.async_invoke(self, ctx)
                    return await _process_result([])
            ctx.fail('Missing command.')

        # Fetch args back out
        args = ctx.protected_args + ctx.args
        ctx.args = []
        ctx.protected_args = []

        if not self.chain:
            with ctx:
                cmd_name, cmd, args = self.resolve_command(ctx, args)
                ctx.invoked_subcommand = cmd_name
                await Command.async_invoke(self, ctx)
                sub_ctx = cmd.make_context(cmd_name, args, parent=ctx)
                with sub_ctx:
                    return await _process_result(await sub_ctx.command.async_invoke(sub_ctx))

        with ctx:
            ctx.invoked_subcommand = args and '*' or None
            await Command.async_invoke(self, ctx)

            contexts = []
            while args:
                cmd_name, cmd, args = self.resolve_command(ctx, args)
                sub_ctx = cmd.make_context(cmd_name, args, parent=ctx,
                                           allow_extra_args=True,
                                           allow_interspersed_args=False)
                contexts.append(sub_ctx)
                args, sub_ctx.args = sub_ctx.args, []

            rv = []
            for sub_ctx in contexts:
                with sub_ctx:
                    rv.append(await sub_ctx.command.async_invoke(sub_ctx))
            return _process_result(rv)

    def parse_args(self, ctx, args):
        import abot.bot
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            event: abot.bot.MessageEvent = abot.bot.current_event.get()
            tbd_tasks.append(event.reply(ctx.get_help()))
            ctx.exit()
        super().parse_args(ctx, args)


class MultiCommand(AsyncMultiCommandMixin, click.MultiCommand):
    pass


class AsyncGroupMixin(AsyncMultiCommandMixin):
    def command(self, *args, **kwargs):
        kwargs.setdefault('cls', Command)
        return super().command(*args, **kwargs)

    def group(self, *args, **kwargs):
        kwargs.setdefault('cls', Group)
        return super().command(*args, **kwargs)

    def invoke(self, ctx):
        return MultiCommand.invoke(self, ctx)

    async def async_invoke(self, ctx):
        return await MultiCommand.async_invoke(self, ctx)


class Group(AsyncGroupMixin, click.Group):
    pass


class AsyncCommandCollection(AsyncMultiCommandMixin):
    async def async_message(self, message: 'abot.bot.MessageEvent'):
        args = shlex.split(message.text)
        if not args:
            return
        prog_name = args.pop(0)
        try:
            try:
                with self.make_context(prog_name, args) as ctx:
                    for task in list(tbd_tasks):
                        await task
                        tbd_tasks.remove(task)
                    await self.async_invoke(ctx)
            except click.ClickException as e:
                for task in list(tbd_tasks):
                    await task
                    tbd_tasks.remove(task)
                await message.reply(e.format_message())
            except click.Abort:
                for task in list(tbd_tasks):
                    await task
                    tbd_tasks.remove(task)
                await message.reply('Aborted!')
        except ExitCode as e:
            for task in list(tbd_tasks):
                await task
                tbd_tasks.remove(task)
            logger.debug(f'Command exited {e}', exc_info=True)
            if e.code:
                message.reply('Exception happened, contact developers')


class CommandCollection(AsyncCommandCollection, click.CommandCollection):
    pass


def command(name=None, **attrs):
    attrs.setdefault('cls', Command)
    return click.command(name, **attrs)


def group(name=None, **attrs):
    attrs.setdefault('cls', Group)
    return click.group(name, **attrs)
