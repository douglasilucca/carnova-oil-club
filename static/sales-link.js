(function () {
  function showFeedback(message) {
    var el = document.getElementById("sales-link-feedback");
    if (el) {
      el.textContent = message;
    }
  }

  function copyReferralLink(url) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(
        function () { showFeedback("Link copied!"); },
        function () { showFeedback("Copy failed. Please copy the link manually."); }
      );
    } else {
      showFeedback("Copy not supported. Please copy the link manually.");
    }
  }

  function shareReferralLink(url) {
    if (navigator.share) {
      navigator.share({
        title: "Carnova Oil Club",
        text: "Save on oil changes with Carnova Oil Club. Use my personal link:",
        url: url,
      }).catch(function () {});
    } else {
      copyReferralLink(url);
    }
  }

  window.carnovaCopyReferralLink = copyReferralLink;
  window.carnovaShareReferralLink = shareReferralLink;
})();
