{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.roomcast;
  settings = pkgs.writeText "roomcast.json" (
    builtins.toJSON {
      roku_ip = cfg.rokuAddress;
      roku_serial = cfg.rokuSerial;
      lan_interface = cfg.lanInterface;
      lan_port = cfg.port;
      discovery_port = cfg.discoveryPort;
      discovery_networks = cfg.discoveryNetworks;
      roku_mac = cfg.rokuMac;
      ip_command = "${pkgs.iproute2}/bin/ip";
      chromium = "${pkgs.chromium}/bin/chromium";
      ffmpeg = "${pkgs.ffmpeg-headless}/bin/ffmpeg";
      ffprobe = "${pkgs.ffmpeg-headless}/bin/ffprobe";
      max_height = cfg.maxHeight;
      allowed_hosts = cfg.allowedMediaHosts;
      site_url = cfg.siteUrl;
      directory_url = cfg.directoryUrl;
      directory_cache = "/var/cache/roomcast/sources.json";
    }
  );
in
{
  options.services.roomcast = {
    enable = lib.mkEnableOption "Roomcast Roku playback";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
    };
    rokuAddress = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Last known IPv4 address, used only as a discovery hint.";
    };
    rokuSerial = lib.mkOption {
      type = lib.types.str;
      description = "Expected Roku serial number, verified before control commands.";
    };
    lanInterface = lib.mkOption {
      type = lib.types.str;
      description = "LAN interface used for discovery and media; independent of its DHCP address.";
    };
    rokuMac = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional TV MAC address for neighbor-cache discovery; serial verification remains required.";
    };
    discoveryNetworks = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Private IPv4 CIDRs searched on port 8060 only after cached addresses and SSDP fail; maximum 1024 addresses total.";
    };
    discoveryPort = lib.mkOption {
      type = lib.types.port;
      default = 18794;
      description = "UDP reply port for SSDP discovery on the LAN interface.";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 18795;
      description = "Media-only LAN port; requests require a session token and the discovered TV source address.";
    };
    directoryUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://www.bestfreestreaming.org/";
      description = "Trusted source-directory URL, read as data on demand.";
    };
    siteUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://cinejoy.to";
      description = "Origin of a site compatible with the Cinejoy resolver, for domain migrations. This does not add support for arbitrary website layouts.";
    };
    allowedMediaHosts = lib.mkOption {
      type = lib.types.nullOr (lib.types.listOf lib.types.str);
      default = null;
      description = "Optional exact media hostname allowlist. Null permits public HTTPS hosts discovered by the resolver; private DNS answers and literal IP URLs are always rejected.";
    };
    maxHeight = lib.mkOption {
      type = lib.types.enum [
        720
        1080
      ];
      default = 1080;
    };
  };
  config = lib.mkIf cfg.enable {
    users.groups.roomcast = { };
    users.users.roomcast = {
      isSystemUser = true;
      group = "roomcast";
    };
    environment.systemPackages = [ cfg.package ];
    systemd.services.roomcast = {
      description = "Roomcast playback relay";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
        "roomcast.socket"
      ];
      requires = [ "roomcast.socket" ];
      wants = [ "network-online.target" ];
      environment = {
        HOME = "/var/lib/roomcast";
        XDG_CACHE_HOME = "/var/cache/roomcast";
        SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
        PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
      };
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/roomcast-service --config ${settings}";
        User = "roomcast";
        Group = "roomcast";
        Sockets = [ "roomcast.socket" ];
        RuntimeDirectory = "roomcast";
        RuntimeDirectoryMode = "0750";
        StateDirectory = "roomcast";
        CacheDirectory = "roomcast";
        UMask = "0007";
        Restart = "on-failure";
        RestartSec = 3;
        TimeoutStopSec = 15;
        MemoryMax = "2G";
        TasksMax = 256;
        CPUQuota = "200%";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
        ];
      };
    };
    systemd.sockets.roomcast = {
      description = "Roomcast media-only LAN socket";
      wantedBy = [ "sockets.target" ];
      listenStreams = [ "0.0.0.0:${toString cfg.port}" ];
      socketConfig = {
        BindToDevice = cfg.lanInterface;
        NoDelay = true;
      };
    };
    networking.firewall.interfaces.${cfg.lanInterface} = {
      allowedTCPPorts = [ cfg.port ];
      allowedUDPPorts = [ cfg.discoveryPort ];
    };
  };
}
