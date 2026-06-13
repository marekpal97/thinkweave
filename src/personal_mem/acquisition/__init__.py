"""Acquisition layer — the discover → drain producer/consumer spine.

- ``sources``   — source-type registry, queues, extractors (the atomic units)
- ``discover``  — producer rail: strategies that emit queue items / plans
- ``importers`` — bulk historical imports (claude-code, chatgpt, files)
"""
