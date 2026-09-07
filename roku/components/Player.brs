sub init()
    m.video = m.top.FindNode("video")
    m.message = m.top.FindNode("message")
    m.subtitleTimer = m.top.FindNode("subtitleTimer")
    m.video.seekMode = "accurate"
    m.video.ObserveField("state", "playerState")
    m.video.ObserveField("availableSubtitleTracks", "applySubtitles")
    m.video.ObserveField("currentSubtitleTrack", "reportSubtitles")
    m.video.ObserveField("globalCaptionMode", "reportSubtitles")
    m.subtitleTimer.ObserveField("fire", "pollSubtitles")
    m.top.SignalBeacon("AppLaunchComplete")
end sub

function textValue(value) as string
    if type(value) = "String" or type(value) = "roString" then return value
    return ""
end function

sub handleRequest()
    request = m.top.request
    if request = invalid then return
    if request.a = "subtitles"
        configureSubtitles(request)
    else if request.a = "seek"
        if m.video.state <> "playing" and m.video.state <> "paused" then return
        number = textValue(request.positionSeconds)
        if not CreateObject("roRegex", "^[0-9]+$", "").IsMatch(number) then return
        target = Val(number)
        if target < 0 or target >= m.video.duration then return
        m.video.autoplayAfterSeek = (request.stayPaused <> "true")
        m.video.seek = target
    else if textValue(request.u) <> ""
        m.pending = request
        if m.video.state = "playing" or m.video.state = "paused" or m.video.state = "buffering"
            m.video.control = "stop"
        else if m.video.state <> "stopping"
            startVideo()
        end if
    end if
end sub

sub startVideo()
    if m.pending = invalid then return
    request = m.pending
    m.pending = invalid
    m.subtitleTimer.control = "stop"
    m.subtitleConfig = invalid
    content = CreateObject("roSGNode", "ContentNode")
    content.title = textValue(request.videoName)
    content.url = request.u
    content.streamFormat = "hls"
    content.playStart = Val(textValue(request.startSeconds))
    tracks = ParseJson(textValue(request.subtitleTracks), "i")
    if type(tracks) = "roArray" then content.subtitleTracks = tracks
    m.video.autoplayAfterSeek = true
    m.video.content = content
    m.playbackReportUrl = textValue(request.playbackReportUrl)
    m.startReported = false
    configureSubtitles(request)
    m.message.visible = false
    m.video.visible = true
    m.video.SetFocus(true)
    m.video.control = "play"
end sub

sub playerState()
    state = m.video.state
    if state = "stopped"
        startVideo()
    else if state = "playing"
        m.video.autoplayAfterSeek = true
        if m.startReported <> true and m.playbackReportUrl <> ""
            m.startReported = true
            report = {startup_seconds: m.video.timeToStartStreaming}
            info = m.video.playStartInfo
            if info <> invalid
                if info.manifest_dur <> invalid then report.manifest_seconds = info.manifest_dur
                if info.prebuf_dur <> invalid then report.buffer_seconds = info.prebuf_dur
            end if
            m.startReporter = CreateObject("roSGNode", "subtitleReport")
            m.startReporter.url = m.playbackReportUrl
            m.startReporter.report = report
            m.startReporter.control = "RUN"
        end if
    else if state = "error" or state = "finished"
        m.subtitleTimer.control = "stop"
        m.video.visible = false
        m.message.visible = true
        if state = "error"
            m.message.text = "Playback failed. Ask Roomcast to try the title again."
        else
            m.message.text = "Roomcast"
        end if
    end if
end sub

sub configureSubtitles(request)
    m.subtitleTimer.control = "stop"
    m.subtitleConfig = {
        subtitlesEnabled: request.subtitlesEnabled <> "false"
        subtitleRequest: textValue(request.subtitleRequest)
        subtitleId: textValue(request.subtitleId)
        subtitleName: textValue(request.subtitleName)
        subtitleUrl: textValue(request.subtitleUrl)
        subtitleReportUrl: textValue(request.subtitleReportUrl)
    }
    m.lastSubtitleReport = ""
    m.subtitlePending = true
    m.subtitleReportSequence = 0
    m.subtitlePolls = 32
    if m.subtitleReporter <> invalid then m.subtitleReporter.control = "STOP"
    applySubtitles()
    if m.subtitlePending then m.subtitleTimer.control = "start"
end sub

sub pollSubtitles()
    m.subtitlePolls--
    applySubtitles()
    if m.subtitlePolls <= 0 or m.subtitlePending <> true
        m.subtitlePending = false
        m.subtitleTimer.control = "stop"
    end if
end sub

sub applySubtitles()
    if m.subtitleConfig = invalid or m.applyingSubtitles = true then return
    if m.subtitlePending <> true
        reportSubtitles()
        return
    end if
    m.applyingSubtitles = true
    if m.subtitleConfig.subtitlesEnabled
        m.video.globalCaptionMode = "On"
        trackName = roomcastSubtitleTrack(m.video.availableSubtitleTracks, m.subtitleConfig)
        if trackName <> "" and m.video.subtitleTrack <> trackName
            m.video.subtitleTrack = trackName
        end if
    else
        m.video.globalCaptionMode = "Off"
    end if
    m.applyingSubtitles = false
    reportSubtitles()
end sub

sub reportSubtitles()
    if m.subtitleConfig = invalid or m.applyingSubtitles = true then return
    if m.subtitleConfig.subtitleReportUrl = "" or m.subtitleConfig.subtitleRequest = "" then return
    report = roomcastSubtitleState(m.video, m.subtitleConfig)
    if report.applied
        m.subtitlePending = false
        m.subtitleTimer.control = "stop"
    end if
    signature = FormatJson(report)
    if signature = m.lastSubtitleReport then return
    m.lastSubtitleReport = signature
    m.subtitleReportSequence++
    report.sequence = m.subtitleReportSequence
    m.subtitleReporter = CreateObject("roSGNode", "subtitleReport")
    m.subtitleReporter.url = m.subtitleConfig.subtitleReportUrl
    m.subtitleReporter.report = report
    m.subtitleReporter.control = "RUN"
end sub
