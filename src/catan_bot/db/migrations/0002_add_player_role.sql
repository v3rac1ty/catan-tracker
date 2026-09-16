-- Optional role mentioned for event notifications.
-- NULL means event creation and reminders do not mention a role.

ALTER TABLE guild_config
    ADD COLUMN player_role_id BIGINT;
