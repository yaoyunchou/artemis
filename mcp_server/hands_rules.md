# Artemis Hands

Drive the phone yourself with the `artemis-hands` tools. Artemis does not choose the next tap. Do not call `mobile_run_task`.

1. Call `list_devices`. When more than one device is connected, ask which serial to use, then `use_device`.
2. Call `look` before every action. The text is a numbered element list starting at 1, and the image is the current screen.
3. Prefer an element index (`click` `target` as an integer). An index is valid only for the latest successful `look`.
4. When the target is not in the list, read the screenshot and pass normalized `[x, y]` coordinates on a 0–1000 scale.
5. After each action, `look` again and decide the next step from that screen.
6. Bracket one task line with `begin_row` (optional `how`: script / ai / replay / manual) and `close_row` or `end_row`. While the row is open, actions inherit that `title` and `in_row: true`. `run_id`, `run_seq`, `pkg`, and `intent` stay on the line.
7. `end_row` looks, logs `outcome` (`done` / `fail` / `skip` / `lost`), and still closes a miss so the next row can start. `learned` means the sheet looked finished. It does not write the catalog unless `ARTEMIS_HANDS_END_ROW_WRITE=1`. Use `close_row` when the outcome is already known and another look would be wrong.
8. `click` / `press_key` take an optional `role`. Empty infers `nav` / `permission` / `cold_start` from a blacklist, and `task` only inside an open row. An explicit `role` wins.
9. `replay_row` logs `replay_start` / `replay_step` / `replay_end` with `outcome` and `fail_kind` (`script` / `env` / `timeout` / `look`). Reasons 「任务弹层没开」 and 「看屏失败」 do not increment `script_fails` and do not clear steps.
