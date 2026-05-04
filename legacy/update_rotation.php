<?php
$file = __DIR__ . '/rotations.json';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $stream = $_POST['stream'] ?? '';
    $rotation = intval($_POST['rotation'] ?? 0);

    if ($stream !== '') {
        $data = [];
        if (file_exists($file)) {
            $json = file_get_contents($file);
            $data = json_decode($json, true) ?: [];
        }
        $data[$stream] = $rotation;
        file_put_contents($file, json_encode($data));
        echo "OK";
    } else {
        http_response_code(400);
        echo "Missing stream";
    }
}
?>
