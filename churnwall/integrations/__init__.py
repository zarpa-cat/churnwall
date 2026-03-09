"""Churnwall integrations: Resend (email) and Slack (alerts).

Usage:
    from churnwall.integrations.dispatcher import IntegrationDispatcher
    from churnwall.settings import settings

    dispatcher = IntegrationDispatcher.from_settings(settings)
    await dispatcher.dispatch(subscriber, recommendation)
"""
