# Artemis Hands

Drive the phone yourself with the `artemis-hands` tools. Artemis does not choose the next tap. Do not call `mobile_run_task`.

1. Call `list_devices`. When more than one device is connected, ask which serial to use, then `use_device`.
2. Call `look` before every action. The text is a numbered element list starting at 1, and the image is the current screen.
3. Prefer an element index (`click` `target` as an integer). An index is valid only for the latest successful `look`.
4. When the target is not in the list, read the screenshot and pass normalized `[x, y]` coordinates on a 0–1000 scale.
5. After each action, `look` again and decide the next step from that screen.
