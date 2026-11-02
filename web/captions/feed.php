<?php
// Church Translate — feed endpoint (Bluehost -> viewers). Public read: captions
// are meant for the room, so no token. The viewer polls this ~1x/second with
// ?since=<rev>; when nothing has changed the response is a tiny {"changed":false}
// so a quiet service costs almost nothing.

require __DIR__ . '/config.php';
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, no-cache, must-revalidate');

$since = isset($_GET['since']) ? (int) $_GET['since'] : -1;

$store = ['epoch' => 0, 'rev' => 0, 'segs' => []];
if (is_file(STORE_FILE)) {
    $d = json_decode((string) file_get_contents(STORE_FILE), true);
    if (is_array($d) && isset($d['segs'])) $store = $d;
}

if ($since === (int) $store['rev']) {
    echo json_encode(['changed' => false, 'rev' => $store['rev'], 'epoch' => $store['epoch']]);
    exit;
}

// The source words and their times (src, t) stay in the store for the archive;
// viewers don't need them, so they aren't sent on every poll.
$segs = array_map(function ($s) { unset($s['src'], $s['t']); return $s; }, (array) $store['segs']);
echo json_encode([
    'changed' => true,
    'rev'     => $store['rev'],
    'epoch'   => $store['epoch'],
    'segs'    => $segs,
], JSON_UNESCAPED_UNICODE);
