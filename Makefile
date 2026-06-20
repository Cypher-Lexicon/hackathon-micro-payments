.PHONY: sync-js

# Copy the browser script to clipboard for pasting into Owncast admin.
# Owncast's CSP blocks external scripts; the Custom JavaScript field
# expects raw JS code and wraps it with the correct nonce internally.
sync-js:
	curl -s http://localhost:8081/static/owncast-pay.js | pbcopy
	@echo "✓ Copied static/owncast-pay.js to clipboard."
	@echo "  Paste into Owncast admin → Customize → Custom JavaScript."
