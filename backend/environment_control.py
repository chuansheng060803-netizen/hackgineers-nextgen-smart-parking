def needs_ventilation(zone):
    risk = zone.get("risk")

    if risk == "High":
        return True

    return False

def get_fans_in_zone(fans, zone_name):
    zone_fans = []

    for fan in fans:
        if fan.get("zoneParent") == zone_name:
            zone_fans.append(fan)

    return zone_fans

def is_fan_operational(fan):
    return (
        fan.get("broken") == False
        and fan.get("isUnderMaintenance") == False
    )


def get_operational_fans_in_zone(fans, zone_name):
    zone_fans = get_fans_in_zone(fans, zone_name)

    operational_fans = []

    for fan in zone_fans:
        if is_fan_operational(fan):
            operational_fans.append(fan)

    return operational_fans


def control_zone_ventilation(client, zone, fans):
    zone_name = zone.get("name")

    operational_fans = get_operational_fans_in_zone(
        fans,
        zone_name
    )

    for fan in operational_fans:
        fan_name = fan.get("name")
        fan_is_on = fan.get("isOn")

        if needs_ventilation(zone):
            if not fan_is_on:
                client.turn_fan_on(fan_name)

        else:
            if fan_is_on:
                client.turn_fan_off(fan_name)


