--!nonstrict
-- UI pieces shared by the Now Playing and Library panels. Palette roles only, spacing on 4/8/12/16.

local format = require("./format.luau")

local M = {}

-- Album art, or a placeholder tile while it downloads (or when there is none).
function M.art(path, size, opts)
  opts = opts or {}
  local radius = opts.round and size / 2 or (opts.radius or 8)
  if path then
    return ui.image({ key = opts.key, path = path, width = size, height = size, radius = radius, fit = "cover" })
  end
  return ui.column({
    key = opts.key,
    width = size,
    height = size,
    radius = radius,
    fill = "surface_container_high",
    align = "center",
    justify = "center",
  }, {
    ui.glyph({ name = opts.glyph or "music", size = math.max(12, math.floor(size * 0.4)), color = "on_surface_variant/0.6" }),
  })
end

function M.explicitBadge()
  return ui.row({ paddingH = 4, radius = 4, fill = "on_surface_variant/0.2", align = "center" }, {
    ui.label({ text = "E", fontSize = 10, fontWeight = "bold", color = "on_surface_variant" }),
  })
end

-- A section heading with an optional trailing control.
function M.sectionHeader(text, trailing)
  local children = {
    ui.label({ text = text, fontWeight = "semibold", color = "on_surface", flexGrow = 1 }),
  }
  if trailing then
    table.insert(children, trailing)
  end
  return ui.row({ gap = 8, align = "center" }, children)
end

-- One media row: art, title (+E), artist, duration, and an optional menu button.
--   opts: key, artPath, artSize (40), dim, onClick, onMenu (left click), onMenuRight (right click: native menu),
--         selected, subtitle (overrides the artist line)
function M.mediaRow(item, opts)
  opts = opts or {}
  local size = opts.artSize or 40
  local titleRow = {
    ui.label({ text = item.title or "", color = opts.dim and "on_surface/0.6" or "on_surface", maxLines = 1, flexGrow = 1 }),
  }
  if item.explicit then
    table.insert(titleRow, M.explicitBadge())
  end
  local children = {
    M.art(opts.artPath, size, { radius = 6, glyph = item.type == "episode" and "microphone" or "music" }),
    ui.column({ gap = 2, flexGrow = 1 }, {
      ui.row({ gap = 6, align = "center" }, titleRow),
      ui.label({ text = opts.subtitle or format.artists(item), fontSize = 12, color = "on_surface_variant", maxLines = 1 }),
    }),
  }
  if item.durationMs then
    table.insert(children, ui.label({ text = format.duration(item.durationMs), fontSize = 12, color = "on_surface_variant" }))
  end
  if opts.onMenu or opts.onMenuRight then
    table.insert(children, ui.button({
      glyph = "dots-vertical",
      variant = "ghost",
      controlSize = "sm",
      onClick = opts.onMenu,
      onRightClick = opts.onMenuRight,
    }))
  end
  return ui.row({
    key = opts.key,
    gap = 12,
    paddingH = 8,
    paddingV = 4,
    radius = 8,
    align = "center",
    fill = opts.selected and "surface_container_high" or nil,
    onClick = opts.onClick,
  }, children)
end

return M
