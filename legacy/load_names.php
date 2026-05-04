<?php
header('Content-Type: application/json');
$namesFile = 'names.json';
if (file_exists($namesFile)) {
    echo file_get_contents($namesFile);
} else {
    // Falls die Datei nicht existiert, gebe Standardwerte aus.
    echo json_encode(array("blue" => "Blue Cheese", "lemon" => "Lemon Haze", "royal" => "Royal Runtz"));
}
?>
