// document.getElementById('shift-generate-btn').addEventListener('click', function() {
//     fetch('/shift_data/')
//     .then(response => response.json())
//     .then(data => {
//         document.getElementById('event_e_A').textContent = data.event_e;
//         document.getElementById('event_f_B').textContent = data.event_f;
//         document.getElementById('event_g_C').textContent = data.event_g;
//     })
//     .catch(error => console.error('Error fetching shift data:', error));
// });

// scripts.js

document.addEventListener('DOMContentLoaded', () => {
    const generateButton = document.getElementById('shift-generate-btn');
    
    generateButton.addEventListener('click', () => {
      // シフト生成処理を実行
      fetch('/shift/generate/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCookie('csrftoken')
        },
        body: JSON.stringify({ /* データ */ })
      })
      .then(response => response.json())
      .then(data => {
        // 結果を表示
        console.log(data);
      });
    });
  });
  