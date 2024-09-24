document.getElementById('generateShift').addEventListener('click', function() {
    fetch('/get-shift-data/')
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.json();
        })
        .then(data => {
            document.getElementById('event_e_A').textContent = `Event E: ${data.event_e}`;
            document.getElementById('event_f_B').textContent = `Event F: ${data.event_f}`;
            document.getElementById('event_g_C').textContent = `Event G: ${data.event_g}`;
        })
        .catch(error => {
            console.error('Error:', error);
        });
});
