--!nonstrict
-- Text helpers shared by the bar and the panels.

local M = {}

-- 83000 -> "1:23", 3723000 -> "1:02:03"
function M.duration(ms)
  if type(ms) ~= "number" then
    return "–:––"
  end
  local s = math.max(0, math.floor(ms / 1000))
  local h, m = s // 3600, (s // 60) % 60
  if h > 0 then
    return string.format("%d:%02d:%02d", h, m, s % 60)
  end
  return string.format("%d:%02d", m, s % 60)
end

-- "40 s" / "10 min" / "1 h" for a wait, rounded up.
function M.wait(ms)
  local s = math.max(1, math.ceil((ms or 0) / 1000))
  if s < 60 then
    return `{s} s`
  end
  local m = math.ceil(s / 60)
  if m < 60 then
    return `{m} min`
  end
  return `{math.ceil(m / 60)} h`
end

function M.artists(item)
  if type(item) ~= "table" then
    return ""
  end
  if item.type == "episode" then
    return (item.podcast and item.podcast.title) or item.author or ""
  end
  local names = {}
  for _, a in ipairs(item.artists or {}) do
    table.insert(names, a.name)
  end
  return table.concat(names, ", ")
end

function M.album(item)
  if type(item) ~= "table" then
    return ""
  end
  if item.type == "episode" then
    return item.author or ""
  end
  return (item.album and item.album.title) or ""
end

return M
