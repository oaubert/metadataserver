var apikey = window.localStorage['metadata-apikey'];
if (apikey) {
    $("#apikey").text("Logged in");
} else {
    $("#apikey").text("No API key");
}

function get_ajax_headers () {
    return {
        "X-APIKey": apikey
    };
}
