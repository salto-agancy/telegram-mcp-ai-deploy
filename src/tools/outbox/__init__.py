"""Outbound work an agent leaves for the owner: drafts, scheduled messages, parking.

Nothing here sends a message to a person. Every write is recorded in the outbox
journal with the agent that made it, so the owner can compare what the agent
wrote, what he changed, and what was actually sent.
"""
