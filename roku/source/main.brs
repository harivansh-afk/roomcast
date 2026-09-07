sub Main(args)
    port = CreateObject("roMessagePort")
    screen = CreateObject("roSGScreen")
    screen.SetMessagePort(port)
    input = CreateObject("roInput")
    input.SetMessagePort(port)
    scene = screen.CreateScene("Player")
    screen.Show()
    scene.request = args
    while true
        event = wait(0, port)
        if type(event) = "roSGScreenEvent"
            if event.IsScreenClosed() then return
        else if type(event) = "roInputEvent"
            if event.IsInput() then scene.request = event.GetInfo()
        end if
    end while
end sub
