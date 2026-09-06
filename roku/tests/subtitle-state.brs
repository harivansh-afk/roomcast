sub main()
    config = {
        subtitleRequest: "request", subtitleId: "english-full"
        subtitleUrl: "http://relay.test/full.srt", subtitleName: "2. English"
        subtitlesEnabled: true
    }
    video = {
        availableSubtitleTracks: [], currentSubtitleTrack: ""
        subtitleTrack: "", globalCaptionMode: "On"
    }
    assertEqual(roomcastSubtitleState(video, config).applied, false, "empty startup list")
    video.availableSubtitleTracks = [
        {TrackName: "native/forced", Description: "1. English"}
        {TrackName: "native/full", Description: "2. English"}
    ]
    assertEqual(roomcastSubtitleTrack(video.availableSubtitleTracks, config), "native/full", "native identifier differs from URL")
    video.subtitleTrack = "native/full"
    video.currentSubtitleTrack = "native/forced"
    assertEqual(roomcastSubtitleState(video, config).applied, false, "requested track is not playing yet")
    video.currentSubtitleTrack = "native/full"
    assertEqual(roomcastSubtitleState(video, config).applied, true, "native selection completed")
    video.globalCaptionMode = "Off"
    assertEqual(roomcastSubtitleState(video, config).applied, false, "remote turns captions off")
    config.subtitlesEnabled = false
    assertEqual(roomcastSubtitleState(video, config).applied, true, "off confirmed")
    config.subtitlesEnabled = true
    video.globalCaptionMode = "On"
    video.availableSubtitleTracks.Push({TrackName: "native/other", Description: "2. English"})
    assertEqual(roomcastSubtitleTrack(video.availableSubtitleTracks, config), "", "ambiguous names refused")
    video.availableSubtitleTracks.Push({TrackName: config.subtitleUrl, Description: "2. English"})
    assertEqual(roomcastSubtitleTrack(video.availableSubtitleTracks, config), config.subtitleUrl, "exact URL takes precedence")
    video.availableSubtitleTracks = [{TRACKNAME: "hls/en", DESCRIPTION: "English"}]
    config.subtitleUrl = ""
    config.subtitleName = "English"
    assertEqual(roomcastSubtitleTrack(video.availableSubtitleTracks, config), "hls/en", "HLS track by name")
    config.subtitleName = "Hindi"
    assertEqual(roomcastSubtitleTrack(video.availableSubtitleTracks, config), "", "no language guessing")
    print "All subtitle state tests passed."
end sub

sub assertEqual(actual, expected, label)
    if actual <> expected
        print "FAIL: "; label; " expected "; expected; " got "; actual
        ' Make failures fatal, including on interpreters without throw support.
        failure = invalid
        failure.abort()
    end if
end sub
