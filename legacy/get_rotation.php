<?php
$file = __DIR__ . '/rotations.json';
if (file_exists($file)) {
    echo file_get_contents($file);
} else {
    echo json_encode(["stream1" => 0, "stream2" => 0]);
}
?>
