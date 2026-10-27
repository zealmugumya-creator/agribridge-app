# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""AgriBridge backend service modules.

Integrations are isolated behind these modules (mandate Phase 10) so the Flask
app stays thin and each external system has its own timeouts, retries, and
failure path.
"""
