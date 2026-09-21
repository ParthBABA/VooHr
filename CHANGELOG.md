# Changelog

All notable changes to this project are tracked here going forward.

## Unreleased

- Meeting Tracker + Conversational Memory backend (Phase 2–8):
  - Meetings: dedicated `GET /api/meetings/<id>/prep` preparation endpoint with
    confirmed/suggested openers, discussion points, pending vs. HR-owned
    commitments, follow-ups and overdue follow-ups; `preparation_status`
    lifecycle (`not_started` → `in_progress` → `completed`) with validation.
  - Conversation memory: additive provenance (`created_by`,
    `confirmation_status`, `metadata`), archive support, and an explicit
    follow-up lifecycle (`priority`, `owner_user_id`, `related_follow_up_id`,
    append-only `status_history`, `IN_PROGRESS`/`CANCELLED` states with
    started/completed/cancelled timestamps). Memory is never auto-completed;
    statuses only change by explicit action.
  - Manager-role scoping extended to conversation-memory reads/writes and the
    notifications list/get/read/dismiss/read-all (fail-closed, like the
    existing employees/meetings scoping).
  - Notifications: one-time meeting reschedule/cancel events, one-time
    `memory_overdue` notifications for past-due commitments/follow-ups (never
    auto-advancing the record), and delivery bookkeeping fields
    (`delivery_status`, `delivery_channel`, `attempts`, `delivery_errors`,
    `next_attempt_at`, `recipient_user_id`) plus a single `delivery_failed`
    notice after external delivery is exhausted.
  - Reminder delivery retries with compare-and-swap claims (multi-worker safe),
    bounded exponential backoff, and an opt-in background sweep daemon
    (`REMINDER_SWEEP_ENABLED`, `REMINDER_SWEEP_INTERVAL_SECONDS`,
    `REMINDER_MAX_ATTEMPTS`). Surfacing now includes in-progress items and
    always excludes archived records.
  - `IN_PROGRESS` items surface in reminder generation, the dashboard open-item
    counters, and the prep endpoint.
  - New unique partial index dedups meeting-event/overdue notifications per
    (org, meeting, event_key); account deletion now cascades to meetings and
    conversation_memory.
  - Fixed: meeting-create audit event referenced an undefined variable (PII of
    the audit payload was the only casualty — record is now written correctly).
- Removed unreferenced `static/product-3.png` (~1.6 MB) left after the webp image
  migration; the `.webp` equivalent is unaffected.
- Removed 8 unreferenced `*.png` originals left after the webp image migration
  (`access-bg`, `aes-bg`, `audit-bg`, `privacy-bg`, `product-1`, `product-2`,
  `security-bg`, `totp-background`), reclaiming ~11.7 MB. Their `.webp`
  equivalents remain referenced and unaffected.
- Initial change-tracking convention: no entries yet in this section.

## Convention going forward

- **Track changes via commit messages and PR descriptions**, not patch files.
- Ad-hoc `*.patch` / `*.diff` files are not committed to the repository (see `.gitignore`).
- Describe user-visible behavior and rationale in commit/PR descriptions so history is self-documenting.
- Keep meaningful product/behavior changes summarized under "Unreleased" (or a dated release section) as they land.
