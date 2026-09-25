(function() {
  var ACTIVITY_TYPES = ['translation_ready', 'audio_ready', 'session_ready'];
  var MEETING_TYPES = ['meeting_reminder', 'meeting_event', 'memory_overdue', 'delivery_failed'];

  function categoryOf(n) {
    if (n && (n.category === 'activity' || n.category === 'meeting' || n.category === 'risk')) return n.category;
    var type = n && n.type;
    if (ACTIVITY_TYPES.indexOf(type) !== -1) return 'activity';
    if (MEETING_TYPES.indexOf(type) !== -1) return 'meeting';
    return 'risk';
  }

  function targetUrl(n) {
    var category = categoryOf(n);
    if (category === 'activity') {
      var query = [];
      if (n.source_session_id) query.push('session_id=' + encodeURIComponent(n.source_session_id));
      if (n.employee_id) query.push('employee_id=' + encodeURIComponent(n.employee_id));
      return '/workspace' + (query.length ? '?' + query.join('&') : '');
    }
    if (category === 'meeting') return '/meeting-tracker';
    return '/risk-drift?notification=' + encodeURIComponent(n.id);
  }

  window.VooNotif = window.VooNotif || {};
  window.VooNotif.categoryOf = categoryOf;
  window.VooNotif.targetUrl = targetUrl;
})();
