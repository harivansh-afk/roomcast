' Report native caption state without blocking playback or ECP input handling.
sub init()
    m.top.functionName = "sendReport"
end sub

sub sendReport()
    port = CreateObject("roMessagePort")
    transfer = CreateObject("roUrlTransfer")
    transfer.SetMessagePort(port)
    transfer.SetUrl(m.top.url)
    transfer.AddHeader("Content-Type", "application/json")
    body = FormatJson(m.top.report)
    for attempt = 1 to 3
        if transfer.AsyncPostFromString(body)
            event = wait(2000, port)
            if type(event) = "roUrlEvent"
                if event.GetResponseCode() = 204 then return
                if event.GetResponseCode() = 404 or event.GetResponseCode() = 409 then return
            else
                transfer.AsyncCancel()
            end if
        end if
    end for
end sub
