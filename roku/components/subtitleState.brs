' Native track identifiers need not be the sidecar download URL.
function roomcastSubtitleTrack(tracks, config) as string
    if type(tracks) <> "roArray" then return ""
    namedTrack = ""
    nameMatches = 0
    for each item in tracks
        trackName = ""
        description = ""
        for each key in item
            if LCase(key) = "trackname" then trackName = item[key]
            if LCase(key) = "description" then description = item[key]
        end for
        if config.subtitleUrl <> "" and trackName = config.subtitleUrl then return trackName
        if config.subtitleName <> "" and description = config.subtitleName and trackName <> ""
            namedTrack = trackName
            nameMatches++
        end if
    end for
    ' Do not silently choose the first of two tracks with the same label.
    if nameMatches = 1 then return namedTrack
    return ""
end function

function roomcastSubtitleState(video, config) as object
    trackName = roomcastSubtitleTrack(video.availableSubtitleTracks, config)
    available = trackName <> ""
    if config.subtitlesEnabled
        applied = available and video.currentSubtitleTrack = trackName and video.globalCaptionMode = "On"
    else
        applied = video.globalCaptionMode = "Off"
    end if
    return {
        request: config.subtitleRequest
        enabled: config.subtitlesEnabled
        track: config.subtitleId
        available: available
        applied: applied
        caption_mode: video.globalCaptionMode
    }
end function
