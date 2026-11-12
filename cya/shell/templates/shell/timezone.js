(function (root) {
  "use strict";

  var formatterCache = new Map();

  function formatter(zone) {
    if (!formatterCache.has(zone)) {
      formatterCache.set(zone, new Intl.DateTimeFormat("en-US-u-ca-gregory-nu-latn", {
        timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
      }));
    }
    return formatterCache.get(zone);
  }

  function wallAt(epoch, zone) {
    var parts = {};
    formatter(zone).formatToParts(new Date(epoch)).forEach(function (part) {
      if (part.type !== "literal") { parts[part.type] = Number(part.value); }
    });
    return {year: parts.year, month: parts.month, day: parts.day,
      hour: parts.hour, minute: parts.minute, second: parts.second};
  }

  function wallStamp(parts) {
    return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
  }

  function sameWall(left, right) {
    return left.year === right.year && left.month === right.month && left.day === right.day &&
      left.hour === right.hour && left.minute === right.minute && left.second === right.second;
  }

  function offsetAt(epoch, zone) {
    var wholeSecond = Math.floor(epoch / 1000) * 1000;
    return wallStamp(wallAt(wholeSecond, zone)) - wholeSecond;
  }

  function parseWall(text) {
    var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(text);
    if (!match) { throw new Error("Choose a valid agency-local date and time."); }
    var parts = {year: Number(match[1]), month: Number(match[2]), day: Number(match[3]),
      hour: Number(match[4]), minute: Number(match[5]), second: 0};
    var date = new Date(wallStamp(parts));
    if (!sameWall(wallAt(date.getTime(), "UTC"), parts)) {
      throw new Error("Choose a valid agency-local date and time.");
    }
    return parts;
  }

  function transitionAfterGap(localStamp, zone, start, end, beforeOffset, afterOffset) {
    if (afterOffset <= beforeOffset) { return null; }
    var low = start, high = end;
    while (high - low > 1000) {
      var middle = low + Math.floor((high - low) / 2);
      if (offsetAt(middle, zone) === beforeOffset) { low = middle; }
      else { high = middle; }
    }
    var gapStart = high + beforeOffset;
    var gapEnd = high + afterOffset;
    return localStamp >= gapStart && localStamp < gapEnd ? high : null;
  }

  function toUtc(text, zone) {
    if (typeof zone !== "string" || !zone.trim()) {
      throw new Error("The agency time zone is unavailable. Contact an administrator.");
    }
    try {
      formatter(zone).format(0);
    } catch (_error) {
      throw new Error("The agency time zone is unavailable. Contact an administrator.");
    }
    var requested = parseWall(text), localStamp = wallStamp(requested);
    // Collect offsets around the entered wall time. Exact round trips identify normal and
    // repeated wall times; sorting chooses the first occurrence, as TIME-3 requires.
    var radius = 48 * 60 * 60 * 1000, step = 30 * 60 * 1000;
    var start = localStamp - radius, end = localStamp + radius;
    var offsets = new Set(), transitions = [], previousAt = start;
    var previousOffset = offsetAt(previousAt, zone);
    offsets.add(previousOffset);
    for (var at = start + step; at <= end; at += step) {
      var currentOffset = offsetAt(at, zone);
      offsets.add(currentOffset);
      if (currentOffset !== previousOffset) {
        transitions.push({start: previousAt, end: at, before: previousOffset, after: currentOffset});
      }
      previousAt = at;
      previousOffset = currentOffset;
    }

    var candidates = [];
    offsets.forEach(function (offset) {
      var candidate = localStamp - offset;
      if (sameWall(wallAt(candidate, zone), requested)) { candidates.push(candidate); }
    });
    if (candidates.length) { return new Date(Math.min.apply(Math, candidates)).toISOString(); }

    // TIME-3 maps a nonexistent wall time to the exact first valid instant after its gap.
    for (var index = 0; index < transitions.length; index += 1) {
      var transition = transitions[index];
      var gap = transitionAfterGap(localStamp, zone, transition.start, transition.end,
        transition.before, transition.after);
      if (gap !== null) { return new Date(gap).toISOString(); }
    }
    throw new Error("That agency-local time could not be resolved. Check the date and time zone.");
  }

  function formatUtc(iso, zone, precision) {
    var date = new Date(iso);
    if (isNaN(date.getTime())) { return ""; }
    if (typeof zone !== "string" || !zone.trim()) {
      throw new Error("The agency time zone is unavailable. Contact an administrator.");
    }
    try {
      var fields = {}, showSeconds = precision === "seconds";
      var formatOptions = {
        timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hourCycle: "h23",
        timeZoneName: "short"
      };
      if (showSeconds) { formatOptions.second = "2-digit"; }
      new Intl.DateTimeFormat("en-CA-u-ca-gregory-nu-latn", formatOptions).formatToParts(date).forEach(function (part) {
        if (part.type !== "literal") { fields[part.type] = part.value; }
      });
      var offset = {};
      new Intl.DateTimeFormat("en-US", {timeZone: zone, timeZoneName: "shortOffset"})
        .formatToParts(date).forEach(function (part) {
          if (part.type === "timeZoneName") { offset.name = part.value; }
        });
      return fields.year + "-" + fields.month + "-" + fields.day + " " +
        fields.hour + ":" + fields.minute + (showSeconds ? ":" + fields.second : "") + " " +
        fields.timeZoneName + " (" + offset.name + ")";
    } catch (_error) {
      throw new Error("The agency time zone is unavailable. Contact an administrator.");
    }
  }

  root.CyaTimezones = {formatUtc: formatUtc, toUtc: toUtc};
})(typeof window === "undefined" ? globalThis : window);
