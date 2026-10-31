--!nonstrict
-- Album art cache: downloads thumbnailUrl into pluginDataDir()/art/<id>.jpg (ui.image takes local files
-- only), at most 4 downloads at a time, keeping about 80 files (the oldest by mtime go first).
-- URLs are fetched as given, as the Sonolith spec asks.

local M = {}

local MAX_FILES = 80
local MAX_ACTIVE = 4
local PRUNE_EVERY = 10

local dir = nil
local queue = {}
local waiting = {} -- path -> { callbacks }
local active = 0
local downloaded = 0

local function artDir()
  if not dir then
    local base = noctalia.pluginDataDir()
    if not base then
      return nil
    end
    dir = base .. "/art"
    noctalia.mkdirAll(dir)
  end
  return dir
end

local function safeId(id)
  return (string.gsub(id, "[^%w_%-]", "_"))
end

function M.pathFor(id)
  local d = artDir()
  return d and `{d}/{safeId(id)}.jpg` or nil
end

-- A cached file for this id, or nil; never downloads.
function M.cached(id)
  local path = id and M.pathFor(id)
  if path and noctalia.fileExists(path) then
    return path
  end
  return nil
end

function M.prune()
  local d = artDir()
  local names = d and noctalia.listDir(d)
  if not names or #names <= MAX_FILES then
    return
  end
  local files = {}
  for _, name in ipairs(names) do
    local path = `{d}/{name}`
    local info = noctalia.fileInfo(path)
    if info and not info.isDir then
      table.insert(files, { path = path, mtime = info.mtime })
    end
  end
  table.sort(files, function(a, b)
    return a.mtime < b.mtime
  end)
  for i = 1, #files - MAX_FILES do
    noctalia.removeFile(files[i].path)
  end
end

local function finish(path, ok)
  local cbs = waiting[path] or {}
  waiting[path] = nil
  for _, cb in ipairs(cbs) do
    cb(ok and path or nil)
  end
end

local function pump()
  while active < MAX_ACTIVE and #queue > 0 do
    local job = table.remove(queue, 1)
    local part = job.path .. ".part"
    active += 1
    local started = noctalia.download(job.url, part, function(ok)
      active -= 1
      -- Rename from .part, so a half-written file is never shown or taken for a cached one.
      if ok then
        ok = noctalia.renameFile(part, job.path)
      else
        noctalia.removeFile(part)
      end
      downloaded += 1
      if downloaded % PRUNE_EVERY == 0 then
        M.prune()
      end
      finish(job.path, ok)
      pump()
    end)
    if not started then
      active -= 1
      finish(job.path, false)
    end
  end
end

-- cb(path | nil). Calls back at once for a cached file; joins a download already on its way.
-- `front` puts the job ahead of the queue (the current track before library thumbnails).
function M.fetch(id, url, cb, front)
  local path = id and M.pathFor(id)
  if not path or type(url) ~= "string" or url == "" then
    cb(nil)
    return
  end
  if noctalia.fileExists(path) then
    cb(path)
    return
  end
  if waiting[path] then
    table.insert(waiting[path], cb)
    return
  end
  waiting[path] = { cb }
  local job = { url = url, path = path }
  if front then
    table.insert(queue, 1, job)
  else
    table.insert(queue, job)
  end
  pump()
end

-- Drops queued (not started) downloads, e.g. when a panel closes.
function M.clearQueue()
  for _, job in ipairs(queue) do
    finish(job.path, false)
  end
  queue = {}
end

return M
