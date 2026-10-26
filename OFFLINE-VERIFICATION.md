# Offline verification boundary

CI now runs the existing synthetic FFmpeg preview/approval/render/ffprobe/release chain.

## Reproduce

`python -m unittest discover -s plugins/videomontazhka/skills/videomontazhka/tests -p test_end_to_end.py -v`

Requires ffmpeg/ffprobe, Pillow 11.3.0 and NumPy 2.2.6. No recording, external media, transcription or paid generation.

## Rollback

Review the commit diff and use a new checkout of the pre-change commit for source rollback. Keep user data and live installations untouched. A source rollback is not permission to restore unsafe network exposure or bypass approval.
