--!nonstrict
-- Now Playing panel. Phase 1: the connection screens (connect, pending, denied, blocked, ...) and a
-- minimal player used to test commands; the full player comes in phase 3.

local S = require("./lib/state.luau")
local commands = require("./lib/commands.luau")
local format = require("./lib/format.luau")

commands.init("nowplaying")

local busy = false -- a connect/disconnect request is on its way
local lastError = nil

local function tr(key, subst)
  return noctalia.tr(key, subst)
end

---- Pieces

local function title(text)
  return ui.label({ text = text, fontSize = 15, fontWeight = "semibold", color = "on_surface" })
end

local function body(text)
  return ui.label({ text = text, color = "on_surface_variant", maxLines = 4 })
end

local function header()
  return ui.row({ gap = 8, align = "center" }, {
    ui.glyph({ name = "music", size = 18, color = "primary" }),
    ui.label({ text = tr("np.title"), fontWeight = "semibold", color = "on_surface", flexGrow = 1 }),
  })
end

local function credit()
  return ui.label({ text = tr("np.usesSonolith"), fontSize = 11, color = "on_surface_variant" })
end

-- A centered status card: glyph, title, text, buttons.
local function statusScreen(glyph, heading, text, buttons, glyphColor)
  local children = {
    ui.glyph({ name = glyph, size = 36, color = glyphColor or "primary" }),
    title(heading),
  }
  if text then
    table.insert(children, body(text))
  end
  if lastError then
    table.insert(children, ui.label({ text = lastError, color = "error", fontSize = 12, maxLines = 3 }))
  end
  if buttons and #buttons > 0 then
    table.insert(children, ui.row({ gap = 8, justify = "center" }, buttons))
  end
  return ui.column({ gap = 12, padding = 16, align = "center", fill = "surface_container_high", radius = 12 }, children)
end

local function button(text, glyph, handler, variant, enabled)
  return ui.button({
    text = text,
    glyph = glyph,
    variant = variant or "default",
    enabled = enabled ~= false and not busy,
    onClick = handler,
  })
end

---- Actions

local function render() end -- forward declaration, defined below

local function request(method, params)
  busy = true
  lastError = nil
  render()
  commands.send(method, params, function(ok, _, err)
    busy = false
    if not ok and err and err.name ~= "RegistrationDenied" and err.name ~= "Blocked" then
      lastError = err.name == "Unavailable" and tr("np.unavailable.title") or tr("np.error.generic", { name = err.name or "?" })
    end
    render()
  end)
end

function onConnect()
  request("bridge.register")
end

function onCancelConnect()
  request("bridge.cancelRegister")
end

function onRetry()
  request("bridge.retry")
end

function onDisconnect()
  request("bridge.disconnect")
end

function onOpenSonolith()
  noctalia.runAsync({ "sonolith" })
end

function onToggle()
  commands.send("player.toggle")
end

function onPrevious()
  commands.send("player.previous")
end

function onNext()
  commands.send("player.next")
end

---- Screens

local function waitLeft(conn)
  if not conn.retryAt then
    return 0
  end
  return math.max(0, conn.retryAt - noctalia.nowMs())
end

local function connectionScreen(conn)
  local state = conn.state
  if state == "error" and conn.message == "noPython" then
    return statusScreen("alert-triangle", tr("np.noPython.title"), tr("np.noPython.body"), nil, "error")
  elseif state == "error" and conn.message == "unsupportedVersion" then
    return statusScreen("alert-triangle", tr("np.unsupported.title"), tr("np.unsupported.body"), nil, "error")
  elseif state == "error" then
    return statusScreen("alert-triangle", tr("np.error.title"), conn.message, nil, "error")
  elseif state == "starting" then
    return statusScreen("loader-2", tr("np.starting.title"), nil, nil)
  elseif state == "unavailable" then
    return statusScreen("music-off", tr("np.unavailable.title"), tr("np.unavailable.body"), {
      button(tr("np.openSonolith"), "external-link", "onOpenSonolith", "primary"),
    }, "on_surface_variant")
  elseif state == "unregistered" then
    return statusScreen("plug-connected", tr("np.unregistered.title"), tr("np.unregistered.body"), {
      button(tr("np.connect"), "plug-connected", "onConnect", "primary"),
    })
  elseif state == "pending" then
    local text = conn.kind == "identity" and tr("np.pending.identity") or tr("np.pending.body")
    local buttons = conn.kind ~= "identity" and { button(tr("np.cancel"), nil, "onCancelConnect", "outline") } or nil
    return statusScreen("hourglass", tr("np.pending.title"), text, buttons)
  elseif state == "denied" or state == "expired" then
    local left = waitLeft(conn)
    local heading = state == "denied" and tr("np.denied.title") or tr("np.expired.title")
    local text = left > 0 and tr("np.denied.wait", { time = format.wait(left) }) or tr("np.denied.ready")
    local handler = conn.kind == "identity" and "onRetry" or "onConnect"
    return statusScreen("ban", heading, text, {
      button(tr("np.tryAgain"), "refresh", handler, "primary", left <= 0),
    }, "on_surface_variant")
  elseif state == "blocked" then
    return statusScreen("lock", tr("np.blocked.title"), tr("np.blocked.body"), {
      button(tr("np.tryAgain"), "refresh", "onConnect", "outline"),
    }, "error")
  elseif state == "disconnected" then
    return statusScreen("plug-connected-x", tr("np.disconnected.title"), tr("np.disconnected.body"), {
      button(tr("np.connect"), "plug-connected", "onConnect", "primary"),
    }, "on_surface_variant")
  end
  return statusScreen("help", state or "?", nil, nil)
end

-- Phase 1 stand-in for the player.
local function playerScreen()
  local item = S.track()
  local player = S.get(S.PLAYER)
  local canControl = S.hasScope("player:control")
  local playing = type(player) == "table" and player.status == "playing"
  local pos, dur = S.positionNow()

  local info
  if item then
    info = ui.column({ gap = 2 }, {
      ui.label({ text = item.title, fontWeight = "semibold", color = "on_surface", maxLines = 2 }),
      ui.label({ text = format.artists(item), color = "on_surface_variant" }),
      ui.label({ text = `{format.duration(pos)} / {format.duration(dur)}`, fontSize = 12, color = "on_surface_variant" }),
    })
  else
    info = body(tr("np.nothingPlaying"))
  end

  return ui.column({ gap = 12 }, {
    ui.column({ gap = 12, padding = 16, fill = "surface_container_high", radius = 12 }, {
      info,
      ui.row({ gap = 8, justify = "center", visible = canControl }, {
        ui.button({ glyph = "player-skip-back", variant = "ghost", onClick = "onPrevious" }),
        ui.button({ glyph = playing and "player-pause" or "player-play", variant = "primary", controlSize = "lg", onClick = "onToggle" }),
        ui.button({ glyph = "player-skip-forward", variant = "ghost", onClick = "onNext" }),
      }),
    }),
    ui.row({ gap = 8, align = "center" }, {
      credit(),
      ui.spacer({ flexGrow = 1 }),
      ui.button({ text = tr("np.disconnect"), variant = "ghost", enabled = not busy, onClick = "onDisconnect" }),
    }),
  })
end

render = function()
  local conn = S.get(S.CONN) or { state = "starting" }
  local content
  if conn.state == S.CONNECTED then
    content = playerScreen()
  else
    content = ui.column({ gap = 12 }, { connectionScreen(conn), credit() })
  end
  panel.render(ui.column({ gap = 12, padding = 16, fill = "surface_container" }, { header(), content }))
end

---- Lifecycle

local isOpen = false

function onOpen(_context)
  isOpen = true
  lastError = nil
  panel.setWantsSecondTicks(true)
  render()
end

function onClose()
  isOpen = false
end

-- Second ticks: the position and the denial countdown move on.
function update()
  if isOpen then
    render()
  end
end

for _, key in ipairs({ S.CONN, S.SCOPES, S.TRACK, S.PLAYER }) do
  noctalia.state.watch(key, function()
    if isOpen then
      render()
    end
  end)
end
