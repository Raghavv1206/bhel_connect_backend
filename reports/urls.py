from django.urls import path
from reports.views import (
    CampaignBuyersReportView,
    CampaignWaitlistReportView,
    MarketplaceSummaryReportView,
)

urlpatterns = [
    path('campaign/<int:campaign_id>/buyers/', CampaignBuyersReportView.as_view(), name='campaign_buyers_report'),
    path('campaign/<int:campaign_id>/waitlist/', CampaignWaitlistReportView.as_view(), name='campaign_waitlist_report'),
    path('marketplace/', MarketplaceSummaryReportView.as_view(), name='marketplace_summary_report'),
]
