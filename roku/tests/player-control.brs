sub main()
    m.video = {state: "paused", duration: 100, seek: 12, autoplayAfterSeek: true}
    m.top = {request: {a: "seek", positionSeconds: "30", stayPaused: "true"}}
    handleRequest()
    check(m.video.seek = 30 and m.video.autoplayAfterSeek = false, "paused seek resumes playback")

    for each number in ["bad", "", "-1", "100", "1000"]
        m.top.request.positionSeconds = number
        handleRequest()
        check(m.video.seek = 30, "malformed or out-of-range seek moved the player")
    end for
    m.video.state = "buffering"
    m.top.request.positionSeconds = "20"
    handleRequest()
    check(m.video.seek = 30, "seek interrupted buffering")

    m.top.request = {u: "http://relay/media/new-session"}
    handleRequest()
    check(m.video.control = "stop", "replacement did not stop the old stream")
    check(m.pending.u = m.top.request.u, "replacement was lost")
    m.video.state = "stopping"
    m.top.request = {u: "http://relay/media/latest-session"}
    handleRequest()
    check(m.pending.u = m.top.request.u, "latest replacement was lost during stop")
    print "All player control tests passed."
end sub

sub check(condition, message)
    if not condition
        print "FAIL: " + message
        stop
    end if
end sub
