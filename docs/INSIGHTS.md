# Insights

The Insights page (left menu, **Insights**) keeps history that UniFi itself only
shows for a short time. The toolkit records it in its own database, so it is
included in the toolkit's backups.

## Internet availability

Once a minute the toolkit reads the console's health and stores one sample per
internet connection (WAN, WAN2, ...): up or down, latency, IP address, ISP, and
the reason UniFi gives when it is down. A connection counts as up when UniFi
reports it as `ok` and it has an IP address.

- **Available** is the share of samples in the chosen period that were up.
- **Minutes down** is the number of down samples (one per minute).
- The bars show one day each, in the server's time zone. A grey bar is a day
  without samples.
- **Outages** are runs of down samples. An outage also ends when samples stop
  for more than 3 minutes, so a restart of the toolkit does not stretch it.

Minutes in which the toolkit could not read the console are not counted at all.
It can't tell whether the internet was down or only the console was unreachable.
Mail alerts for an unreachable console are on System > Alerts.

Samples are kept for 400 days, so a year can be compared with the previous one.
That is about 525,000 rows per connection per year, roughly 50 MB in SQLite.

## Speed tests

Every 10 minutes the toolkit reads the gateway's last speed test and stores it if
it is new. UniFi only keeps the last one. Speed tests are scheduled or started in
UniFi under Settings > Internet; the toolkit does not start them itself. The page
shows a chart, averages over the period and the full list.

## Firmware

The Firmware tab reads every device's firmware live from the console and shows
which devices have an update waiting and the version it would install.
Updates are installed in UniFi.

Every 10 minutes the toolkit also notes each device's version, and logs a change
when it differs from the last one. The first sighting of a device is not shown
as a change.

## API

| Endpoint | Returns |
| --- | --- |
| `GET /api/insights/wan?days=30` | Availability, daily bars and outages per connection (1 to 400 days) |
| `GET /api/insights/speedtests?days=90` | Speed tests in the period and their averages |
| `GET /api/insights/firmware` | Devices with firmware and update state, and the last 50 changes. Without a console `devices` is null, `error` says why, and the changes are still returned |
