// owncast-pay.js
// Injected via Owncast Admin → Customize → Custom JavaScript.
// Handles wallet connection, EIP-3009 signing, and tier selection.
//
// Flow: reads Owncast frontend's accessToken from localStorage, opens a
// temporary WebSocket to grab user.id from CONNECTED_USER_INFO, then
// calls /lookup/by-user-id on the sidecar to discover the auth session.
// Never calls /api/chat/register — we reuse the Owncast frontend's user.

(function () {
  'use strict'

  // Derive sidecar host from the page origin so remote viewers reach
  // the server's sidecar instead of resolving localhost on their own machine.
  var SIDECAR = window.location.protocol + '//' + window.location.hostname + ':8081'
  var POLL_INTERVAL = 2000
  var MODAL_Z = 99999
  var UID_KEY = '_owncast_pay_uid'

  var authRequestId = null
  var tierCents = null
  var viewerAddress = null

  // ── Identity: read Owncast frontend's accessToken, grab user.id ──

  function waitForAccessToken() {
    var t = localStorage.getItem('accessToken')
    if (t) return Promise.resolve(t)

    return new Promise(function (resolve, reject) {
      var tries = 0
      var poll = setInterval(function () {
        t = localStorage.getItem('accessToken')
        if (t) { clearInterval(poll); resolve(t); return }
        if (++tries >= 120) { clearInterval(poll); reject(new Error('accessToken not found')); }
      }, 500)
    })
  }

  function getUserIdFromWs(accessToken) {
    return new Promise(function (resolve, reject) {
      var url = new URL(window.location.origin)
      url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      url.pathname = '/ws'
      url.searchParams.append('accessToken', accessToken)

      var ws = new WebSocket(url.toString())
      var timer = setTimeout(function () { ws.close(); reject(new Error('WS timeout')) }, 10000)

      ws.onmessage = function (e) {
        var parts = e.data.split('\n')
        for (var i = 0; i < parts.length; i++) {
          try {
            var m = JSON.parse(parts[i])
            if (m.type === 'CONNECTED_USER_INFO' && m.user && m.user.id) {
              clearTimeout(timer)
              ws.close()
              resolve(String(m.user.id))
              return
            }
          } catch (_) {}
        }
      }
      ws.onerror = function () { clearTimeout(timer); reject(new Error('WS error')) }
      ws.onclose = function (e) { if (!e.wasClean) { clearTimeout(timer); reject(new Error('WS closed')) } }
    })
  }

  function getOwncastUserId() {
    var cached
    try { cached = JSON.parse(localStorage.getItem(UID_KEY)) } catch (_) {}
    if (cached && cached.id) return Promise.resolve(cached.id)

    return waitForAccessToken().then(function (token) {
      return getUserIdFromWs(token)
    }).then(function (id) {
      try { localStorage.setItem(UID_KEY, JSON.stringify({ id: id })) } catch (_) {}
      return id
    })
  }

  // ── Sidecar API helpers ──────────────────────────────────────────

  function sidecarGet(path) {
    return fetch(SIDECAR + path).then(function (r) {
      if (!r.ok) throw new Error('GET ' + path + ' -> ' + r.status)
      return r.json()
    })
  }

  function sidecarPost(path, body) {
    return fetch(SIDECAR + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json()
    }).then(function (data) {
      if (!data.ok) throw new Error(data.error || 'POST failed')
      return data
    })
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms) }) }

  // ── Discovery: find our auth_request_id ──────────────────────────

  function discoverSession(userId) {
    var i = 0
    return new Promise(function (resolve, reject) {
      var poll = setInterval(function () {
        sidecarGet('/lookup/by-user-id/' + userId).then(function (data) {
          if (data.found) {
            clearInterval(poll)
            authRequestId = data.auth_request_id
            resolve()
          }
        }).catch(function () {
          // sidecar may not be ready yet
        })
        i++
        if (i >= 60) {  // 2 min max
          clearInterval(poll)
          reject(new Error('No sidecar session found'))
        }
      }, POLL_INTERVAL)
    })
  }

  // ── Session poll loop ────────────────────────────────────────────

  function pollSession() {
    return new Promise(function (resolve, reject) {
      var maxTries = 300  // 10 minutes max
      var tries = 0

      var poll = setInterval(function () {
        sidecarGet('/session/' + authRequestId).then(function (data) {
          if (data.state === 'needs_auth') {
            clearInterval(poll)
            showTierModal(data.tiers, data.viewer_username).then(function (tier) {
              if (tier) {
                tierCents = tier.cents
                doAuthorization(data)
              } else {
                sidecarPost('/decline/' + authRequestId).catch(function () {})
                removeModal()
                showToast('Watching for free')
              }
            })
          }
          if (data.state === 'settled') {
            clearInterval(poll)
            resolve()
          }
        }).catch(function () {})
        tries++
        if (tries >= maxTries) {
          clearInterval(poll)
          reject(new Error('Session polling timed out'))
        }
      }, POLL_INTERVAL)
    })
  }

  // ── Onchain authorization (EIP-3009) ─────────────────────────────

  function doAuthorization(session) {
    // 1. Connect wallet
    if (!window.ethereum) {
      showInstallToast()
      sidecarPost('/decline/' + authRequestId).catch(function () {})
      removeModal()
      showToast('Watching for free (no wallet found)')
      return
    }

    window.ethereum.request({ method: 'eth_requestAccounts' }).then(function (accounts) {
      viewerAddress = accounts[0]
      return buildAndSign(session, viewerAddress)
    }).then(function (authorization) {
      return sidecarPost('/authorize/' + authRequestId, {
        tier_cents: tierCents,
        authorization: authorization,
      })
    }).then(function () {
      removeModal()
      showToast('Authorized! You will be charged ~$' + (tierCents / 100).toFixed(2))
    }).catch(function (err) {
      console.warn('[owncast-pay] Auth failed:', err)
      sidecarPost('/decline/' + authRequestId).catch(function () {})
      removeModal()
      showToast('Watching for free (wallet error)')
    })
  }

  function buildAndSign(session, viewerAddress) {
    var nonce = '0x' + Array.from(crypto.getRandomValues(new Uint8Array(32)))
      .map(function (b) { return b.toString(16).padStart(2, '0') }).join('')

    var value = String(tierCents * 10000)
    var validBefore = String(session.valid_before)

    var message = {
      from: viewerAddress,
      to: session.streamer_wallet,
      value: value,
      validAfter: '0',
      validBefore: validBefore,
      nonce: nonce,
    }

    var typedData = JSON.stringify({
      domain: {
        name: 'USDC',
        version: '2',
        chainId: Number(session.usdc_chain_id),
        verifyingContract: session.usdc_contract,
      },
      types: {
        TransferWithAuthorization: [
          { name: 'from', type: 'address' },
          { name: 'to', type: 'address' },
          { name: 'value', type: 'uint256' },
          { name: 'validAfter', type: 'uint256' },
          { name: 'validBefore', type: 'uint256' },
          { name: 'nonce', type: 'bytes32' },
        ],
      },
      message: message,
      primaryType: 'TransferWithAuthorization',
    })

    showToast('Waiting for wallet signature...')

    return window.ethereum.request({
      method: 'eth_signTypedData_v4',
      params: [viewerAddress, typedData],
    }).then(function (signature) {
      var sig = signature.replace('0x', '')
      var r = '0x' + sig.substring(0, 64)
      var s = '0x' + sig.substring(64, 128)
      var v = parseInt(sig.substring(128, 130), 16).toString(16)
      return {
        from: viewerAddress,
        to: session.streamer_wallet,
        value: value,
        validAfter: '0',
        validBefore: validBefore,
        nonce: nonce,
        v: v,
        r: r,
        s: s,
      }
    })
  }

  // ── UI: Tier selection modal ─────────────────────────────────────

  function showTierModal(tiers, username) {
    removeModal()
    return new Promise(function (resolve) {
      var overlay = document.createElement('div')
      overlay.id = 'owncast-pay-overlay'
      overlay.style.cssText = [
        'position:fixed;inset:0;background:rgba(0,0,0,0.7);',
        'display:flex;align-items:center;justify-content:center;',
        'z-index:' + MODAL_Z + ';font-family:system-ui,sans-serif;',
      ].join('')

      var tierButtons = tiers.map(function (t) {
        var tierLabel = '$' + (t.cents / 100).toFixed(2) + ' for up to ' + t.minutes + ' min'
        return (
          '<button class="ocp-tier-btn" data-cents="' + t.cents + '" style="'
          + 'display:block;width:100%;padding:14px 16px;margin:6px 0;background:#16213e;'
          + 'border:1px solid #333;border-radius:8px;color:#fff;cursor:pointer;'
          + 'font-size:15px;text-align:left;">'
          + '<span style="color:#00d4aa;font-weight:700;">' + tierLabel + '</span>'
          + '<span style="float:right;color:#666;font-size:13px;">'
          + (t.cents / t.minutes * 100).toFixed(1) + ' cents/min</span>'
          + '</button>'
        )
      }).join('')

      overlay.innerHTML = (
        '<div style="background:#1a1a2e;border:1px solid #333;border-radius:12px;'
        + 'padding:28px;max-width:380px;width:90%;color:#fff;box-shadow:0 10px 40px rgba(0,0,0,0.5);">'
        + '<h3 style="margin:0 0 6px;font-size:18px;">Support this stream</h3>'
        + '<p style="margin:0 0 4px;color:#aaa;font-size:14px;">'
        + 'Choose how much to authorize. You will only be charged <strong style="color:#00d4aa;">once</strong>'
        + ' when you leave. No per-minute prompts.</p>'
        + '<p style="margin:0 0 16px;color:#666;font-size:12px;">'
        + 'One wallet signature. Unused time is not refunded.</p>'
        + '<div style="margin-bottom:12px;">' + tierButtons + '</div>'
        + '<button id="ocp-skip" style="width:100%;padding:12px;background:transparent;'
        + 'border:1px solid #444;border-radius:8px;color:#666;cursor:pointer;font-size:14px;">'
        + 'Watch without paying</button>'
        + '</div>'
      )

      document.body.appendChild(overlay)

      overlay.querySelectorAll('.ocp-tier-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
          removeModal()
          resolve({ cents: parseInt(btn.dataset.cents) })
        })
      })

      overlay.querySelector('#ocp-skip').addEventListener('click', function () {
        removeModal()
        resolve(null)
      })
    })
  }

  function removeModal() {
    var el = document.getElementById('owncast-pay-overlay')
    if (el) el.remove()
  }

  // ── UI: Toast notifications ──────────────────────────────────────

  function showToast(message) {
    removeToast()
    var el = document.createElement('div')
    el.id = 'owncast-pay-toast'
    el.style.cssText = [
      'position:fixed;bottom:80px;right:20px;z-index:' + (MODAL_Z + 1) + ';',
      'background:#1a1a2e;color:#00d4aa;border:1px solid #00d4aa;',
      'border-radius:20px;padding:10px 18px;',
      'font-family:monospace;font-size:13px;',
      'max-width:320px;word-wrap:break-word;',
    ].join('')
    el.textContent = message
    document.body.appendChild(el)
    setTimeout(function () { el.remove() }, 5000)
  }

  function removeToast() {
    var el = document.getElementById('owncast-pay-toast')
    if (el) el.remove()
  }

  // ── UI: Persistent banner ────────────────────────────────────────

  function showBanner(text) {
    removeBanner()
    var el = document.createElement('div')
    el.id = 'owncast-pay-banner'
    el.style.cssText = [
      'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);',
      'z-index:' + (MODAL_Z + 1) + ';background:#1a1a2e;color:#ffd700;',
      'border:1px solid #ffd700;border-radius:8px;padding:12px 20px;',
      'font-family:system-ui;font-size:14px;text-align:center;',
      'box-shadow:0 4px 12px rgba(0,0,0,0.4);',
    ].join('')
    el.textContent = text
    document.body.appendChild(el)
  }

  function removeBanner() {
    var el = document.getElementById('owncast-pay-banner')
    if (el) el.remove()
  }

  function showInstallToast() {
    removeToast()
    var el = document.createElement('div')
    el.id = 'owncast-pay-toast'
    el.style.cssText = [
      'position:fixed;bottom:80px;right:20px;z-index:' + (MODAL_Z + 1) + ';',
      'background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:10px;',
      'padding:16px 20px;max-width:280px;font-family:system-ui;font-size:14px;',
    ].join('')
    el.innerHTML = (
      'No wallet found.<br>'
      + '<a href="https://wallet.coinbase.com" target="_blank"'
      + ' style="color:#00d4aa;text-decoration:none;">'
      + 'Get Coinbase Wallet</a> to support this creator.'
    )
    document.body.appendChild(el)
    setTimeout(function () { el.remove() }, 10000)
  }

  // ── Inject animation keyframes ───────────────────────────────────

  ;(function injectKeyframes() {
    if (document.getElementById('ocp-styles')) return
    var s = document.createElement('style')
    s.id = 'ocp-styles'
    s.textContent = (
      '.ocp-tier-btn { transition: border-color 0.15s; } '
      + '.ocp-tier-btn:hover { border-color: #00d4aa !important; } '
      + '@keyframes ocp-fadein { from { opacity:0; transform:translateY(10px) }'
      + ' to { opacity:1; transform:translateY(0) } }'
    )
    document.head.appendChild(s)
  })()

  // ── Main entry ───────────────────────────────────────────────────

  ;(async function () {
    console.log('[owncast-pay] Loading...')

    // Step 1: Get our Owncast user ID via the frontend's accessToken.
    // We read the token Owncast's frontend stores in localStorage,
    // open a one-shot WebSocket to grab user.id from CONNECTED_USER_INFO,
    // then cache it. Never call /api/chat/register ourselves.
    var userId
    try {
      userId = await getOwncastUserId()
      console.log('[owncast-pay] User ID:', userId)
    } catch (err) {
      console.warn('[owncast-pay] Could not get user ID:', err)
      return
    }

    // Step 2: Discover our sidecar session
    try {
      await discoverSession(userId)
      console.log('[owncast-pay] auth_request_id:', authRequestId)
    } catch (err) {
      console.warn('[owncast-pay] Session discovery failed:', err.message)
      showBanner('Sidecar not connected. Start the sidecar and refresh.')
      return
    }

    removeBanner()

    // Step 3: Poll session state
    try {
      await pollSession()
      console.log('[owncast-pay] Session complete')
    } catch (err) {
      console.warn('[owncast-pay] Session poll failed:', err.message)
    }
  })()
})()